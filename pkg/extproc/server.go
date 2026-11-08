package extproc

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"time"

	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"github.com/jeefy/stand-in/pkg/config"
	"github.com/jeefy/stand-in/pkg/metrics"
	"github.com/jeefy/stand-in/pkg/patterns"
	"github.com/jeefy/stand-in/pkg/restorer"
	"github.com/jeefy/stand-in/pkg/tokenizer"
	"github.com/jeefy/stand-in/pkg/turns"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/structpb"
)

const MetadataKey = "standin"

type Restorer interface {
	Feed(chunk []byte) []byte
	Flush() []byte
	Abandon(chunk []byte) []byte
	Held() int
	RestoredCount() int
}

type passthroughRestorer struct{}

func (p *passthroughRestorer) Feed(chunk []byte) []byte    { return chunk }
func (p *passthroughRestorer) Flush() []byte              { return nil }
func (p *passthroughRestorer) Abandon(chunk []byte) []byte { return chunk }
func (p *passthroughRestorer) Held() int                  { return 0 }
func (p *passthroughRestorer) RestoredCount() int         { return 0 }

type Server struct {
	extprocv3.UnimplementedExternalProcessorServer
	tokenizer  *tokenizer.Tokenizer
	countRules []patterns.Rule
	cfg        *config.Config
}

func NewServer(tok *tokenizer.Tokenizer, countRules []patterns.Rule, cfg *config.Config) *Server {
	return &Server{
		tokenizer:  tok,
		countRules: countRules,
		cfg:        cfg,
	}
}

type exchange struct {
	srv                  *Server
	mode                 string
	chunkBytes           int
	maxBodyBytes         int
	deferRequestHeaders  bool
	countNew             bool
	outcome              string
	headersPending       bool
	requestAnswered      bool
	oversize             bool
	requestBytes         int
	requestChunks        [][]byte
	result               *tokenizer.TokenizeResult
	newTurnCounts        *tokenizer.NewCounts
	restorer             Restorer
	restoredReported     int
}

func newExchange(srv *Server) *exchange {
	chunkBytes := srv.cfg.ChunkBytes
	if chunkBytes <= 0 {
		chunkBytes = config.DefaultChunkBytes
	}
	return &exchange{
		srv:                 srv,
		mode:                srv.cfg.Mode,
		chunkBytes:          chunkBytes,
		maxBodyBytes:        srv.cfg.MaxBodyBytes,
		deferRequestHeaders: srv.cfg.DeferRequestHeaders,
		countNew:            srv.cfg.CountNew,
		outcome:             "incomplete",
		restorer:            &passthroughRestorer{},
	}
}

func (s *Server) Process(stream extprocv3.ExternalProcessor_ProcessServer) error {
	metrics.ActiveStreams.Inc()
	defer metrics.ActiveStreams.Dec()

	// Send gRPC response headers now: agentgateway times out the ext_proc call until they
	// arrive, and with deferred request headers the first message waits for the whole body.
	if err := stream.SendHeader(metadata.MD{}); err != nil {
		return err
	}

	ex := newExchange(s)
	defer func() {
		metrics.RecordRequest(ex.mode, ex.outcome)
		res := ex.result
		restoredCount := ex.restorer.RestoredCount()

		if res != nil && (res.Replaced > 0 || restoredCount > 0) {
			tiers := res.TierCounts()
			logAttrs := []any{
				"mode", ex.mode,
				"outcome", ex.outcome,
				"tokenized", res.Replaced,
				"distinct", res.Distinct(),
				"tier_a", tiers["a"],
				"tier_b", tiers["b"],
				"restored", restoredCount,
			}
			if ex.newTurnCounts != nil {
				for t, n := range ex.newTurnCounts.TierCounts() {
					logAttrs = append(logAttrs, fmt.Sprintf("new_%s", t), n)
				}
			}
			slog.Info("exchange", logAttrs...)
		}
	}()

	for {
		req, err := stream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}

		phase := phaseName(req)
		responses, handleErr := ex.safeHandle(req, phase)
		if handleErr != nil {
			metrics.RecordError(phase)
			ex.outcome = "error"
			slog.Error("ext_proc phase failed; passing through",
				"phase", phase,
				"error", handleErr,
			)
			if phase == "response_body" {
				if rBody := req.GetResponseBody(); rBody != nil {
					failOpenResp := ex.failOpenResponse(rBody)
					_ = stream.Send(failOpenResp)
				}
			}
			return nil
		}

		for _, resp := range responses {
			if err := stream.Send(resp); err != nil {
				return err
			}
		}
	}
}

func (ex *exchange) safeHandle(req *extprocv3.ProcessingRequest, phase string) (resps []*extprocv3.ProcessingResponse, err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("panic in phase %s: %v", phase, r)
		}
	}()
	return ex.handle(req)
}

func (ex *exchange) failOpenResponse(body *extprocv3.HttpBody) *extprocv3.ProcessingResponse {
	var data []byte
	func() {
		defer func() {
			if r := recover(); r != nil {
				metrics.RecordError("response_abandon")
				data = body.Body
			}
		}()
		data = ex.restorer.Abandon(body.Body)
	}()
	ex.restorer = &passthroughRestorer{}
	return streamedOne(data, body.EndOfStream, false)
}

func phaseName(req *extprocv3.ProcessingRequest) string {
	switch req.Request.(type) {
	case *extprocv3.ProcessingRequest_RequestHeaders:
		return "request_headers"
	case *extprocv3.ProcessingRequest_RequestBody:
		return "request_body"
	case *extprocv3.ProcessingRequest_ResponseHeaders:
		return "response_headers"
	case *extprocv3.ProcessingRequest_ResponseBody:
		return "response_body"
	case *extprocv3.ProcessingRequest_RequestTrailers:
		return "request_trailers"
	case *extprocv3.ProcessingRequest_ResponseTrailers:
		return "response_trailers"
	default:
		return "unknown"
	}
}

func (ex *exchange) handle(req *extprocv3.ProcessingRequest) ([]*extprocv3.ProcessingResponse, error) {
	switch r := req.Request.(type) {
	case *extprocv3.ProcessingRequest_RequestHeaders:
		return ex.handleRequestHeaders(r.RequestHeaders)
	case *extprocv3.ProcessingRequest_RequestBody:
		return ex.handleRequestBody(r.RequestBody)
	case *extprocv3.ProcessingRequest_ResponseHeaders:
		return ex.handleResponseHeaders(r.ResponseHeaders)
	case *extprocv3.ProcessingRequest_ResponseBody:
		return ex.handleResponseBody(r.ResponseBody)
	case *extprocv3.ProcessingRequest_RequestTrailers:
		return []*extprocv3.ProcessingResponse{
			{Response: &extprocv3.ProcessingResponse_RequestTrailers{RequestTrailers: &extprocv3.TrailersResponse{}}},
		}, nil
	case *extprocv3.ProcessingRequest_ResponseTrailers:
		return []*extprocv3.ProcessingResponse{
			{Response: &extprocv3.ProcessingResponse_ResponseTrailers{ResponseTrailers: &extprocv3.TrailersResponse{}}},
		}, nil
	default:
		return nil, nil
	}
}

func (ex *exchange) handleRequestHeaders(hdr *extprocv3.HttpHeaders) ([]*extprocv3.ProcessingResponse, error) {
	if !hdr.EndOfStream && ex.deferRequestHeaders {
		ex.headersPending = true
		return nil, nil
	}
	if hdr.EndOfStream {
		ex.requestAnswered = true
		ex.outcome = "no_body"
	}
	return []*extprocv3.ProcessingResponse{ex.requestHeadersResponse()}, nil
}

func (ex *exchange) handleRequestBody(body *extprocv3.HttpBody) ([]*extprocv3.ProcessingResponse, error) {
	if ex.oversize {
		return []*extprocv3.ProcessingResponse{streamedOne(body.Body, body.EndOfStream, true)}, nil
	}
	if ex.requestAnswered {
		return nil, fmt.Errorf("request body after request leg answered")
	}

	if len(body.Body) > 0 {
		ex.requestChunks = append(ex.requestChunks, body.Body)
		ex.requestBytes += len(body.Body)
	}

	if ex.maxBodyBytes > 0 && ex.requestBytes > ex.maxBodyBytes {
		return ex.overflow(body.EndOfStream), nil
	}

	if !body.EndOfStream {
		return nil, nil
	}

	original := bytesJoin(ex.requestChunks)
	ex.requestChunks = nil
	outgoing := original

	tokenized, err := ex.tokenize(original)
	if err != nil {
		metrics.RecordError("request_body")
		ex.outcome = "error"
		ex.result = nil
	} else {
		outgoing = tokenized
	}

	ex.requestAnswered = true
	var out []*extprocv3.ProcessingResponse
	if ex.headersPending {
		ex.headersPending = false
		out = append(out, ex.requestHeadersResponse())
	}
	out = append(out, streamed(outgoing, ex.chunkBytes, true, true)...)
	return out, nil
}

func (ex *exchange) tokenize(body []byte) ([]byte, error) {
	metrics.BodyBytes.Observe(float64(len(body)))
	start := time.Now()

	result, err := ex.srv.tokenizer.TokenizeJSON(body)
	elapsed := time.Since(start).Seconds()
	metrics.TokenizeSeconds.Observe(elapsed)

	if ex.countNew {
		ex.recordCountNew(body)
	}

	if err != nil {
		return body, err
	}
	if result == nil {
		ex.outcome = "skipped_non_json"
		return body, nil
	}

	ex.result = result
	for _, hit := range result.Hits {
		metrics.RecordMatch(hit.Tier, hit.Index)
	}

	if result.Replaced == 0 {
		ex.outcome = "clean"
		return body, nil
	}

	ex.outcome = "tokenized"
	if ex.mode != "enforce" {
		return body, nil
	}
	return []byte(result.Text), nil
}

func (ex *exchange) recordCountNew(body []byte) {
	var doc any
	if err := json.Unmarshal(body, &doc); err != nil {
		metrics.TurnScopeTotal.WithLabelValues("none").Inc()
		return
	}
	scope, texts := turns.NewTurnStrings(doc)
	metrics.TurnScopeTotal.WithLabelValues(string(scope)).Inc()

	joined := strings.Join(texts, tokenizer.LiteralSep)
	deadline := time.Now().Add(2 * time.Second)
	spans, err := ex.srv.tokenizer.FindSpans(joined, ex.srv.countRules, &deadline)
	if err != nil {
		metrics.RecordError("count_new")
		return
	}

	var hits []tokenizer.Hit
	for _, span := range spans {
		hits = append(hits, span.Hits...)
		for _, hit := range span.Hits {
			metrics.RecordNewMatch(hit.Tier, hit.Index)
		}
	}
	ex.newTurnCounts = &tokenizer.NewCounts{
		Scope: string(scope),
		Hits:  hits,
	}
}

func (ex *exchange) overflow(endOfStream bool) []*extprocv3.ProcessingResponse {
	buffered := bytesJoin(ex.requestChunks)
	ex.requestChunks = nil
	ex.oversize = true
	ex.requestAnswered = true
	ex.outcome = "oversize"
	ex.result = nil

	var out []*extprocv3.ProcessingResponse
	if ex.headersPending {
		ex.headersPending = false
		out = append(out, ex.requestHeadersResponse())
	}
	out = append(out, streamed(buffered, ex.chunkBytes, true, endOfStream)...)
	return out
}

func (ex *exchange) handleResponseHeaders(hdr *extprocv3.HttpHeaders) ([]*extprocv3.ProcessingResponse, error) {
	if !ex.requestAnswered {
		ex.outcome = "no_body"
		ex.requestAnswered = true
	}
	ex.chooseRestorer(hdr)
	return []*extprocv3.ProcessingResponse{
		{
			Response: &extprocv3.ProcessingResponse_ResponseHeaders{
				ResponseHeaders: &extprocv3.HeadersResponse{
					Response: &extprocv3.CommonResponse{},
				},
			},
			DynamicMetadata: ex.metadata(),
		},
	}, nil
}

func (ex *exchange) chooseRestorer(headers *extprocv3.HttpHeaders) {
	if ex.mode != "enforce" || ex.result == nil || len(ex.result.Mapping) == 0 {
		return
	}

	headersMap := make(map[string]string)
	if headers != nil && headers.Headers != nil {
		for _, h := range headers.Headers.Headers {
			val := h.Value
			if len(h.RawValue) > 0 {
				val = string(h.RawValue)
			}
			headersMap[strings.ToLower(h.Key)] = strings.ToLower(val)
		}
	}

	if enc, ok := headersMap["content-encoding"]; ok && enc != "" && enc != "identity" {
		metrics.RecordError("response_encoded")
		return
	}

	ct := headersMap["content-type"]
	if strings.Contains(ct, "text/event-stream") {
		ex.restorer = restorer.NewSseRestorer(ex.result.Mapping)
	} else if isJSONContentType(ct) {
		ex.restorer = restorer.NewJsonRestorer(ex.result.Mapping, restorer.DefaultMaxBufferBytes)
	} else {
		ex.restorer = restorer.NewByteRestorer(ex.result.Mapping)
	}
}

func (ex *exchange) handleResponseBody(body *extprocv3.HttpBody) ([]*extprocv3.ProcessingResponse, error) {
	out := ex.restorer.Feed(body.Body)
	if body.EndOfStream {
		out = append(out, ex.restorer.Flush()...)
	}

	metrics.HeldBytes.Observe(float64(ex.restorer.Held()))
	ex.reportRestored()

	return streamed(out, ex.chunkBytes, false, body.EndOfStream), nil
}

func (ex *exchange) reportRestored() {
	current := ex.restorer.RestoredCount()
	delta := current - ex.restoredReported
	if delta > 0 {
		metrics.RestoredTotal.Add(float64(delta))
		ex.restoredReported = current
	}
}

func (ex *exchange) requestHeadersResponse() *extprocv3.ProcessingResponse {
	return &extprocv3.ProcessingResponse{
		Response: &extprocv3.ProcessingResponse_RequestHeaders{
			RequestHeaders: &extprocv3.HeadersResponse{
				Response: &extprocv3.CommonResponse{
					HeaderMutation: &extprocv3.HeaderMutation{
						RemoveHeaders: []string{"accept-encoding"},
					},
				},
			},
		},
		DynamicMetadata: ex.metadata(),
	}
}

func (ex *exchange) metadata() *structpb.Struct {
	fields := make(map[string]any)
	fields["mode"] = ex.mode
	if ex.result != nil {
		fields["tokenized"] = float64(ex.result.Replaced)
		fields["distinct"] = float64(ex.result.Distinct())
		tiers := ex.result.TierCounts()
		fields["tier_a"] = float64(tiers["a"])
		fields["tier_b"] = float64(tiers["b"])
	} else {
		fields["tokenized"] = float64(0)
		fields["distinct"] = float64(0)
		fields["tier_a"] = float64(0)
		fields["tier_b"] = float64(0)
	}

	if ex.newTurnCounts != nil {
		for t, n := range ex.newTurnCounts.TierCounts() {
			fields[fmt.Sprintf("new_%s", t)] = float64(n)
		}
	}

	s, err := structpb.NewStruct(map[string]any{
		MetadataKey: fields,
	})
	if err != nil {
		return &structpb.Struct{}
	}
	return s
}

func isJSONContentType(ct string) bool {
	media := ct
	if idx := strings.IndexByte(ct, ';'); idx != -1 {
		media = ct[:idx]
	}
	media = strings.TrimSpace(media)
	return media == "application/json" || strings.HasSuffix(media, "+json")
}

func streamedOne(data []byte, endOfStream bool, isRequest bool) *extprocv3.ProcessingResponse {
	bodyResp := &extprocv3.BodyResponse{
		Response: &extprocv3.CommonResponse{
			BodyMutation: &extprocv3.BodyMutation{
				Mutation: &extprocv3.BodyMutation_StreamedResponse{
					StreamedResponse: &extprocv3.StreamedBodyResponse{
						Body:        data,
						EndOfStream: endOfStream,
					},
				},
			},
		},
	}
	if isRequest {
		return &extprocv3.ProcessingResponse{
			Response: &extprocv3.ProcessingResponse_RequestBody{
				RequestBody: bodyResp,
			},
		}
	}
	return &extprocv3.ProcessingResponse{
		Response: &extprocv3.ProcessingResponse_ResponseBody{
			ResponseBody: bodyResp,
		},
	}
}

func streamed(data []byte, chunkBytes int, isRequest bool, endOfStream bool) []*extprocv3.ProcessingResponse {
	if chunkBytes <= 0 {
		chunkBytes = config.DefaultChunkBytes
	}
	if len(data) <= chunkBytes {
		return []*extprocv3.ProcessingResponse{streamedOne(data, endOfStream, isRequest)}
	}

	var out []*extprocv3.ProcessingResponse
	for start := 0; start < len(data); start += chunkBytes {
		end := start + chunkBytes
		last := false
		if end >= len(data) {
			end = len(data)
			last = true
		}
		piece := data[start:end]
		out = append(out, streamedOne(piece, endOfStream && last, isRequest))
	}
	return out
}

func bytesJoin(chunks [][]byte) []byte {
	n := 0
	for _, c := range chunks {
		n += len(c)
	}
	b := make([]byte, n)
	pos := 0
	for _, c := range chunks {
		copy(b[pos:], c)
		pos += len(c)
	}
	return b
}
