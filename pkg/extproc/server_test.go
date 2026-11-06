package extproc

import (
	"context"
	"net"
	"strings"
	"testing"
	"time"

	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"github.com/jeefy/stand-in/pkg/config"
	"github.com/jeefy/stand-in/pkg/patterns"
	"github.com/jeefy/stand-in/pkg/tokenizer"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

func TestExtProcStream(t *testing.T) {
	rules, err := patterns.LoadRules("../patterns/patterns.yaml")
	if err != nil {
		t.Fatal(err)
	}
	countRules, err := patterns.LoadCountRules("../patterns/patterns.yaml")
	if err != nil {
		t.Fatal(err)
	}

	cfg := &config.Config{
		Mode:                "enforce",
		Salt:                "extproc-test-salt",
		ChunkBytes:          1024,
		MaxBodyBytes:        1024 * 1024,
		DeferRequestHeaders: true,
		CountNew:            true,
	}

	tok, err := tokenizer.NewTokenizer(rules, countRules, cfg.Salt, 100)
	if err != nil {
		t.Fatal(err)
	}

	srv := NewServer(tok, countRules, cfg)

	lis := bufconn.Listen(1024 * 1024)
	grpcServer := grpc.NewServer()
	extprocv3.RegisterExternalProcessorServer(grpcServer, srv)

	go func() {
		_ = grpcServer.Serve(lis)
	}()
	defer grpcServer.Stop()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, err := grpc.DialContext(ctx, "bufnet",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
			return lis.Dial()
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	client := extprocv3.NewExternalProcessorClient(conn)
	stream, err := client.Process(ctx)
	if err != nil {
		t.Fatal(err)
	}

	err = stream.Send(&extprocv3.ProcessingRequest{
		Request: &extprocv3.ProcessingRequest_RequestHeaders{
			RequestHeaders: &extprocv3.HttpHeaders{
				EndOfStream: false,
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	prefix := "gh" + "p_"
	token := prefix + strings.Repeat("1", 36)
	reqJSON := `{"prompt": "deploy key: ` + token + `"}`
	err = stream.Send(&extprocv3.ProcessingRequest{
		Request: &extprocv3.ProcessingRequest_RequestBody{
			RequestBody: &extprocv3.HttpBody{
				Body:        []byte(reqJSON),
				EndOfStream: true,
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	resp1, err := stream.Recv()
	if err != nil {
		t.Fatalf("recv deferred headers: %v", err)
	}
	if resp1.GetRequestHeaders() == nil {
		t.Fatalf("expected request headers response, got %v", resp1)
	}
	meta := resp1.GetDynamicMetadata()
	if meta == nil || meta.Fields["standin"] == nil {
		t.Fatalf("expected standin dynamic metadata in headers, got %v", meta)
	}

	resp2, err := stream.Recv()
	if err != nil {
		t.Fatalf("recv request body: %v", err)
	}
	tokenizedBody := string(resp2.GetRequestBody().GetResponse().GetBodyMutation().GetStreamedResponse().GetBody())
	if !strings.Contains(tokenizedBody, "<<secret-") || strings.Contains(tokenizedBody, token) {
		t.Fatalf("body was not properly tokenized: %s", tokenizedBody)
	}

	err = stream.Send(&extprocv3.ProcessingRequest{
		Request: &extprocv3.ProcessingRequest_ResponseHeaders{
			ResponseHeaders: &extprocv3.HttpHeaders{
				Headers: &corev3.HeaderMap{
					Headers: []*corev3.HeaderValue{
						{Key: "content-type", Value: "application/json"},
					},
				},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	resp3, err := stream.Recv()
	if err != nil {
		t.Fatalf("recv response headers: %v", err)
	}
	if resp3.GetResponseHeaders() == nil {
		t.Fatalf("expected response headers, got %v", resp3)
	}

	placeholderIdx := strings.Index(tokenizedBody, "<<secret-")
	ph := tokenizedBody[placeholderIdx : placeholderIdx+23]

	respJSON := `{"response": "using key ` + ph + `"}`
	err = stream.Send(&extprocv3.ProcessingRequest{
		Request: &extprocv3.ProcessingRequest_ResponseBody{
			ResponseBody: &extprocv3.HttpBody{
				Body:        []byte(respJSON),
				EndOfStream: true,
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	resp4, err := stream.Recv()
	if err != nil {
		t.Fatalf("recv response body: %v", err)
	}
	restoredBody := string(resp4.GetResponseBody().GetResponse().GetBodyMutation().GetStreamedResponse().GetBody())
	if !strings.Contains(restoredBody, token) {
		t.Fatalf("response body did not restore original secret, got: %s", restoredBody)
	}
}

type panickingRestorer struct {
	abandonCalled bool
}

func (p *panickingRestorer) Feed(chunk []byte) []byte {
	panic("simulated restorer feed panic")
}
func (p *panickingRestorer) Flush() []byte {
	return nil
}
func (p *panickingRestorer) Abandon(chunk []byte) []byte {
	p.abandonCalled = true
	return chunk
}
func (p *panickingRestorer) Held() int          { return 0 }
func (p *panickingRestorer) RestoredCount() int { return 0 }

func TestExtProcResponseBodyPanicFailOpen(t *testing.T) {
	cfg := &config.Config{
		Mode:       "enforce",
		ChunkBytes: 0,
	}
	srv := NewServer(nil, nil, cfg)

	lis := bufconn.Listen(1024 * 1024)
	grpcServer := grpc.NewServer()
	extprocv3.RegisterExternalProcessorServer(grpcServer, srv)

	go func() {
		_ = grpcServer.Serve(lis)
	}()
	defer grpcServer.Stop()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, err := grpc.DialContext(ctx, "bufnet",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
			return lis.Dial()
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	client := extprocv3.NewExternalProcessorClient(conn)
	stream, err := client.Process(ctx)
	if err != nil {
		t.Fatal(err)
	}

	ex := newExchange(srv)
	mockRestorer := &panickingRestorer{}
	ex.restorer = mockRestorer

	rawChunk := []byte("plain text response chunk")
	resp := ex.failOpenResponse(&extprocv3.HttpBody{
		Body:        rawChunk,
		EndOfStream: true,
	})
	if !mockRestorer.abandonCalled {
		t.Errorf("expected restorer.Abandon to be called on fail open")
	}
	bodyOut := resp.GetResponseBody().GetResponse().GetBodyMutation().GetStreamedResponse().GetBody()
	if string(bodyOut) != string(rawChunk) {
		t.Errorf("expected fail-open body %q, got %q", string(rawChunk), string(bodyOut))
	}

	err = stream.Send(&extprocv3.ProcessingRequest{
		Request: &extprocv3.ProcessingRequest_RequestHeaders{
			RequestHeaders: &extprocv3.HttpHeaders{
				EndOfStream: true,
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
}

func TestStreamedZeroChunkBytes(t *testing.T) {
	data := []byte("some arbitrary text")
	resps := streamed(data, 0, false, true)
	if len(resps) != 1 {
		t.Fatalf("expected 1 response with chunkBytes=0 fallback, got %d", len(resps))
	}
}
