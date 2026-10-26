import datetime

from standin._proto.envoy.config.core.v3 import base_pb2 as _base_pb2
from standin._proto.envoy.extensions.filters.http.ext_proc.v3 import processing_mode_pb2 as _processing_mode_pb2
from standin._proto.envoy.type.v3 import http_status_pb2 as _http_status_pb2
from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from standin._proto.xds.annotations.v3 import status_pb2 as _status_pb2
from standin._proto.envoy.annotations import deprecation_pb2 as _deprecation_pb2
from standin._proto.udpa.annotations import status_pb2 as _status_pb2_1
from standin._proto.validate import validate_pb2 as _validate_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ProtocolConfiguration(_message.Message):
    __slots__ = ("request_body_mode", "response_body_mode", "send_body_without_waiting_for_header_response")
    REQUEST_BODY_MODE_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_BODY_MODE_FIELD_NUMBER: _ClassVar[int]
    SEND_BODY_WITHOUT_WAITING_FOR_HEADER_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    request_body_mode: _processing_mode_pb2.ProcessingMode.BodySendMode
    response_body_mode: _processing_mode_pb2.ProcessingMode.BodySendMode
    send_body_without_waiting_for_header_response: bool
    def __init__(self, request_body_mode: _Optional[_Union[_processing_mode_pb2.ProcessingMode.BodySendMode, str]] = ..., response_body_mode: _Optional[_Union[_processing_mode_pb2.ProcessingMode.BodySendMode, str]] = ..., send_body_without_waiting_for_header_response: _Optional[bool] = ...) -> None: ...

class ProcessingRequest(_message.Message):
    __slots__ = ("request_headers", "response_headers", "request_body", "response_body", "request_trailers", "response_trailers", "metadata_context", "attributes", "observability_mode", "protocol_config")
    class AttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _struct_pb2.Struct
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...
    REQUEST_HEADERS_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_HEADERS_FIELD_NUMBER: _ClassVar[int]
    REQUEST_BODY_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_BODY_FIELD_NUMBER: _ClassVar[int]
    REQUEST_TRAILERS_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_TRAILERS_FIELD_NUMBER: _ClassVar[int]
    METADATA_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    OBSERVABILITY_MODE_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_CONFIG_FIELD_NUMBER: _ClassVar[int]
    request_headers: HttpHeaders
    response_headers: HttpHeaders
    request_body: HttpBody
    response_body: HttpBody
    request_trailers: HttpTrailers
    response_trailers: HttpTrailers
    metadata_context: _base_pb2.Metadata
    attributes: _containers.MessageMap[str, _struct_pb2.Struct]
    observability_mode: bool
    protocol_config: ProtocolConfiguration
    def __init__(self, request_headers: _Optional[_Union[HttpHeaders, _Mapping]] = ..., response_headers: _Optional[_Union[HttpHeaders, _Mapping]] = ..., request_body: _Optional[_Union[HttpBody, _Mapping]] = ..., response_body: _Optional[_Union[HttpBody, _Mapping]] = ..., request_trailers: _Optional[_Union[HttpTrailers, _Mapping]] = ..., response_trailers: _Optional[_Union[HttpTrailers, _Mapping]] = ..., metadata_context: _Optional[_Union[_base_pb2.Metadata, _Mapping]] = ..., attributes: _Optional[_Mapping[str, _struct_pb2.Struct]] = ..., observability_mode: _Optional[bool] = ..., protocol_config: _Optional[_Union[ProtocolConfiguration, _Mapping]] = ...) -> None: ...

class ProcessingResponse(_message.Message):
    __slots__ = ("request_headers", "response_headers", "request_body", "response_body", "request_trailers", "response_trailers", "immediate_response", "streamed_immediate_response", "dynamic_metadata", "mode_override", "request_drain", "override_message_timeout")
    REQUEST_HEADERS_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_HEADERS_FIELD_NUMBER: _ClassVar[int]
    REQUEST_BODY_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_BODY_FIELD_NUMBER: _ClassVar[int]
    REQUEST_TRAILERS_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_TRAILERS_FIELD_NUMBER: _ClassVar[int]
    IMMEDIATE_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    STREAMED_IMMEDIATE_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    DYNAMIC_METADATA_FIELD_NUMBER: _ClassVar[int]
    MODE_OVERRIDE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_DRAIN_FIELD_NUMBER: _ClassVar[int]
    OVERRIDE_MESSAGE_TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    request_headers: HeadersResponse
    response_headers: HeadersResponse
    request_body: BodyResponse
    response_body: BodyResponse
    request_trailers: TrailersResponse
    response_trailers: TrailersResponse
    immediate_response: ImmediateResponse
    streamed_immediate_response: StreamedImmediateResponse
    dynamic_metadata: _struct_pb2.Struct
    mode_override: _processing_mode_pb2.ProcessingMode
    request_drain: bool
    override_message_timeout: _duration_pb2.Duration
    def __init__(self, request_headers: _Optional[_Union[HeadersResponse, _Mapping]] = ..., response_headers: _Optional[_Union[HeadersResponse, _Mapping]] = ..., request_body: _Optional[_Union[BodyResponse, _Mapping]] = ..., response_body: _Optional[_Union[BodyResponse, _Mapping]] = ..., request_trailers: _Optional[_Union[TrailersResponse, _Mapping]] = ..., response_trailers: _Optional[_Union[TrailersResponse, _Mapping]] = ..., immediate_response: _Optional[_Union[ImmediateResponse, _Mapping]] = ..., streamed_immediate_response: _Optional[_Union[StreamedImmediateResponse, _Mapping]] = ..., dynamic_metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., mode_override: _Optional[_Union[_processing_mode_pb2.ProcessingMode, _Mapping]] = ..., request_drain: _Optional[bool] = ..., override_message_timeout: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ...) -> None: ...

class HttpHeaders(_message.Message):
    __slots__ = ("headers", "attributes", "end_of_stream")
    class AttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _struct_pb2.Struct
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    END_OF_STREAM_FIELD_NUMBER: _ClassVar[int]
    headers: _base_pb2.HeaderMap
    attributes: _containers.MessageMap[str, _struct_pb2.Struct]
    end_of_stream: bool
    def __init__(self, headers: _Optional[_Union[_base_pb2.HeaderMap, _Mapping]] = ..., attributes: _Optional[_Mapping[str, _struct_pb2.Struct]] = ..., end_of_stream: _Optional[bool] = ...) -> None: ...

class HttpBody(_message.Message):
    __slots__ = ("body", "end_of_stream", "end_of_stream_without_message", "grpc_message_compressed")
    BODY_FIELD_NUMBER: _ClassVar[int]
    END_OF_STREAM_FIELD_NUMBER: _ClassVar[int]
    END_OF_STREAM_WITHOUT_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    GRPC_MESSAGE_COMPRESSED_FIELD_NUMBER: _ClassVar[int]
    body: bytes
    end_of_stream: bool
    end_of_stream_without_message: bool
    grpc_message_compressed: bool
    def __init__(self, body: _Optional[bytes] = ..., end_of_stream: _Optional[bool] = ..., end_of_stream_without_message: _Optional[bool] = ..., grpc_message_compressed: _Optional[bool] = ...) -> None: ...

class HttpTrailers(_message.Message):
    __slots__ = ("trailers",)
    TRAILERS_FIELD_NUMBER: _ClassVar[int]
    trailers: _base_pb2.HeaderMap
    def __init__(self, trailers: _Optional[_Union[_base_pb2.HeaderMap, _Mapping]] = ...) -> None: ...

class HeadersResponse(_message.Message):
    __slots__ = ("response",)
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    response: CommonResponse
    def __init__(self, response: _Optional[_Union[CommonResponse, _Mapping]] = ...) -> None: ...

class BodyResponse(_message.Message):
    __slots__ = ("response",)
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    response: CommonResponse
    def __init__(self, response: _Optional[_Union[CommonResponse, _Mapping]] = ...) -> None: ...

class TrailersResponse(_message.Message):
    __slots__ = ("header_mutation",)
    HEADER_MUTATION_FIELD_NUMBER: _ClassVar[int]
    header_mutation: HeaderMutation
    def __init__(self, header_mutation: _Optional[_Union[HeaderMutation, _Mapping]] = ...) -> None: ...

class StreamedImmediateResponse(_message.Message):
    __slots__ = ("headers_response", "body_response", "trailers_response")
    HEADERS_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    BODY_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    TRAILERS_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    headers_response: HttpHeaders
    body_response: StreamedBodyResponse
    trailers_response: _base_pb2.HeaderMap
    def __init__(self, headers_response: _Optional[_Union[HttpHeaders, _Mapping]] = ..., body_response: _Optional[_Union[StreamedBodyResponse, _Mapping]] = ..., trailers_response: _Optional[_Union[_base_pb2.HeaderMap, _Mapping]] = ...) -> None: ...

class CommonResponse(_message.Message):
    __slots__ = ("status", "header_mutation", "body_mutation", "trailers", "clear_route_cache")
    class ResponseStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        CONTINUE: _ClassVar[CommonResponse.ResponseStatus]
        CONTINUE_AND_REPLACE: _ClassVar[CommonResponse.ResponseStatus]
    CONTINUE: CommonResponse.ResponseStatus
    CONTINUE_AND_REPLACE: CommonResponse.ResponseStatus
    STATUS_FIELD_NUMBER: _ClassVar[int]
    HEADER_MUTATION_FIELD_NUMBER: _ClassVar[int]
    BODY_MUTATION_FIELD_NUMBER: _ClassVar[int]
    TRAILERS_FIELD_NUMBER: _ClassVar[int]
    CLEAR_ROUTE_CACHE_FIELD_NUMBER: _ClassVar[int]
    status: CommonResponse.ResponseStatus
    header_mutation: HeaderMutation
    body_mutation: BodyMutation
    trailers: _base_pb2.HeaderMap
    clear_route_cache: bool
    def __init__(self, status: _Optional[_Union[CommonResponse.ResponseStatus, str]] = ..., header_mutation: _Optional[_Union[HeaderMutation, _Mapping]] = ..., body_mutation: _Optional[_Union[BodyMutation, _Mapping]] = ..., trailers: _Optional[_Union[_base_pb2.HeaderMap, _Mapping]] = ..., clear_route_cache: _Optional[bool] = ...) -> None: ...

class ImmediateResponse(_message.Message):
    __slots__ = ("status", "headers", "body", "grpc_status", "details")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    GRPC_STATUS_FIELD_NUMBER: _ClassVar[int]
    DETAILS_FIELD_NUMBER: _ClassVar[int]
    status: _http_status_pb2.HttpStatus
    headers: HeaderMutation
    body: bytes
    grpc_status: GrpcStatus
    details: str
    def __init__(self, status: _Optional[_Union[_http_status_pb2.HttpStatus, _Mapping]] = ..., headers: _Optional[_Union[HeaderMutation, _Mapping]] = ..., body: _Optional[bytes] = ..., grpc_status: _Optional[_Union[GrpcStatus, _Mapping]] = ..., details: _Optional[str] = ...) -> None: ...

class GrpcStatus(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: int
    def __init__(self, status: _Optional[int] = ...) -> None: ...

class HeaderMutation(_message.Message):
    __slots__ = ("set_headers", "remove_headers")
    SET_HEADERS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_HEADERS_FIELD_NUMBER: _ClassVar[int]
    set_headers: _containers.RepeatedCompositeFieldContainer[_base_pb2.HeaderValueOption]
    remove_headers: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, set_headers: _Optional[_Iterable[_Union[_base_pb2.HeaderValueOption, _Mapping]]] = ..., remove_headers: _Optional[_Iterable[str]] = ...) -> None: ...

class StreamedBodyResponse(_message.Message):
    __slots__ = ("body", "end_of_stream", "end_of_stream_without_message", "grpc_message_compressed")
    BODY_FIELD_NUMBER: _ClassVar[int]
    END_OF_STREAM_FIELD_NUMBER: _ClassVar[int]
    END_OF_STREAM_WITHOUT_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    GRPC_MESSAGE_COMPRESSED_FIELD_NUMBER: _ClassVar[int]
    body: bytes
    end_of_stream: bool
    end_of_stream_without_message: bool
    grpc_message_compressed: bool
    def __init__(self, body: _Optional[bytes] = ..., end_of_stream: _Optional[bool] = ..., end_of_stream_without_message: _Optional[bool] = ..., grpc_message_compressed: _Optional[bool] = ...) -> None: ...

class BodyMutation(_message.Message):
    __slots__ = ("body", "clear_body", "streamed_response")
    BODY_FIELD_NUMBER: _ClassVar[int]
    CLEAR_BODY_FIELD_NUMBER: _ClassVar[int]
    STREAMED_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    body: bytes
    clear_body: bool
    streamed_response: StreamedBodyResponse
    def __init__(self, body: _Optional[bytes] = ..., clear_body: _Optional[bool] = ..., streamed_response: _Optional[_Union[StreamedBodyResponse, _Mapping]] = ...) -> None: ...
