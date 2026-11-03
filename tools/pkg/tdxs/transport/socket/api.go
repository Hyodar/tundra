package socket

import (
	"encoding/hex"
	"encoding/json"
	"fmt"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
)

type SocketTransportRequestMethod string

const (
	SocketTransportRequestMethodIssue    SocketTransportRequestMethod = "issue"
	SocketTransportRequestMethodMetadata SocketTransportRequestMethod = "metadata"
	SocketTransportRequestMethodValidate SocketTransportRequestMethod = "validate"
)

type SocketTransportRequest struct {
	Method SocketTransportRequestMethod `json:"method"`
	Data   json.RawMessage              `json:"data"`
}

func (r *SocketTransportRequest) UnmarshalData() (any, error) {
	switch r.Method {
	case SocketTransportRequestMethodIssue:
		var issueRequest SocketTransportIssueRequest
		if err := json.Unmarshal(r.Data, &issueRequest); err != nil {
			return nil, fmt.Errorf("failed to unmarshal issue request: %w", err)
		}
		return issueRequest.ToAPIRequest()
	case SocketTransportRequestMethodMetadata:
		var metadataRequest SocketTransportMetadataRequest
		if err := json.Unmarshal(r.Data, &metadataRequest); err != nil {
			return nil, fmt.Errorf("failed to unmarshal metadata request: %w", err)
		}
		return metadataRequest.ToAPIRequest()
	case SocketTransportRequestMethodValidate:
		var validateRequest SocketTransportValidateRequest
		if err := json.Unmarshal(r.Data, &validateRequest); err != nil {
			return nil, fmt.Errorf("failed to unmarshal validate request: %w", err)
		}
		return validateRequest.ToAPIRequest()
	}
	return nil, fmt.Errorf("invalid method: %s", r.Method)
}

type SocketTransportIssueRequest struct {
	UserData string `json:"userData"`
	Nonce    string `json:"nonce"`
	// Caller, when set, is the tag the client expects: tdxs refuses the request unless the
	// peer is that caller.
	Caller string `json:"caller,omitempty"`
}

func (r *SocketTransportIssueRequest) ToAPIRequest() (*api.IssueRequest, error) {
	userData, err := hex.DecodeString(r.UserData)
	if err != nil {
		return nil, fmt.Errorf("failed to decode user data: %w", err)
	}

	nonce, err := hex.DecodeString(r.Nonce)
	if err != nil {
		return nil, fmt.Errorf("failed to decode nonce: %w", err)
	}

	return &api.IssueRequest{UserData: userData, Nonce: nonce, Caller: r.Caller}, nil
}

type SocketTransportMetadataRequest struct{}

func (r *SocketTransportMetadataRequest) ToAPIRequest() (*api.MetadataRequest, error) {
	return &api.MetadataRequest{}, nil
}

type SocketTransportValidateRequest struct {
	Document string `json:"document"`
	Nonce    string `json:"nonce"`
}

func (r *SocketTransportValidateRequest) ToAPIRequest() (*api.ValidateRequest, error) {
	document, err := hex.DecodeString(r.Document)
	if err != nil {
		return nil, fmt.Errorf("failed to decode document: %w", err)
	}

	nonce, err := hex.DecodeString(r.Nonce)
	if err != nil {
		return nil, fmt.Errorf("failed to decode nonce: %w", err)
	}

	return &api.ValidateRequest{Document: document, Nonce: nonce}, nil
}

type SocketTransportIssueResponseData struct {
	Document string `json:"document"`
}

type SocketTransportIssueResponse struct {
	Data  *SocketTransportIssueResponseData `json:"data"`
	Error *string                           `json:"error"`
}

func NewIssueResponseFromError(err error) *SocketTransportIssueResponse {
	errStr := fmt.Sprintf("transport error: %v", err)
	return &SocketTransportIssueResponse{
		Error: &errStr,
	}
}

func NewIssueResponseFromAPI(response *api.IssueResponse) *SocketTransportIssueResponse {
	if response.Error != nil {
		errStr := fmt.Sprintf("issuer error: %v", response.Error)
		return &SocketTransportIssueResponse{
			Error: &errStr,
		}
	}

	return &SocketTransportIssueResponse{
		Data: &SocketTransportIssueResponseData{
			Document: hex.EncodeToString(response.Document),
		},
	}
}

type SocketTransportMetadataResponseData struct {
	IssuerType string `json:"issuerType"`
	UserData   string `json:"userData"`
	Nonce      string `json:"nonce"`
	Metadata   any    `json:"metadata"`
}

type SocketTransportMetadataResponse struct {
	Data  *SocketTransportMetadataResponseData `json:"data"`
	Error *string                              `json:"error"`
}

func NewMetadataResponseFromError(err error) *SocketTransportMetadataResponse {
	errStr := fmt.Sprintf("transport error: %v", err)
	return &SocketTransportMetadataResponse{
		Error: &errStr,
	}
}

func NewMetadataResponseFromAPI(response *api.MetadataResponse) *SocketTransportMetadataResponse {
	if response.Error != nil {
		errStr := fmt.Sprintf("metadata error: %v", response.Error)
		return &SocketTransportMetadataResponse{
			Error: &errStr,
		}
	}

	return &SocketTransportMetadataResponse{
		Data: &SocketTransportMetadataResponseData{
			IssuerType: response.IssuerType,
			UserData:   hex.EncodeToString(response.UserData),
			Nonce:      hex.EncodeToString(response.Nonce),
			Metadata:   response.Metadata,
		},
	}
}

type SocketTransportValidateResponseData struct {
	UserData string `json:"userData"`
	Valid    bool   `json:"valid"`
}

type SocketTransportValidateResponse struct {
	Data  *SocketTransportValidateResponseData `json:"data"`
	Error *string                              `json:"error"`
}

func NewValidateResponseFromError(err error) *SocketTransportValidateResponse {
	errStr := fmt.Sprintf("transport error: %v", err)
	return &SocketTransportValidateResponse{
		Error: &errStr,
	}
}

func NewValidateResponseFromAPI(response *api.ValidateResponse) *SocketTransportValidateResponse {
	if response.Error != nil {
		errStr := fmt.Sprintf("validator error: %v", response.Error)
		return &SocketTransportValidateResponse{
			Error: &errStr,
		}
	}

	return &SocketTransportValidateResponse{
		Data: &SocketTransportValidateResponseData{
			UserData: hex.EncodeToString(response.UserData),
			Valid:    response.Valid,
		},
	}
}
