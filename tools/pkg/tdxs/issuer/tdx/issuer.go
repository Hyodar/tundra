package tdx

import (
	"context"
	"encoding/json"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	tundraissuer "github.com/Hyodar/tundra/tools/pkg/attestation/issuer"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/issuer"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/logger"
)

type TDXIssuer struct {
	issuer.Issuer

	logger  logger.Logger
	backend tundraissuer.Issuer
}

func NewTDXIssuer(logger logger.Logger) (*TDXIssuer, error) {
	backend, err := tundraissuer.NewTDXIssuer()
	if err != nil {
		return nil, err
	}
	return &TDXIssuer{
		backend: backend,
		logger:  logger,
	}, nil
}

func (i *TDXIssuer) Start(_ context.Context) error {
	return nil
}

func (i *TDXIssuer) Issue(ctx context.Context, req *api.IssueRequest) *api.IssueResponse {
	doc, err := i.backend.Issue(ctx, req.UserData, req.Nonce)
	if err != nil {
		return &api.IssueResponse{Error: err}
	}
	docBytes, err := json.Marshal(doc)
	if err != nil {
		return &api.IssueResponse{Error: err}
	}
	return &api.IssueResponse{Document: docBytes}
}

func (i *TDXIssuer) Metadata(ctx context.Context, req *api.MetadataRequest) *api.MetadataResponse {
	measurements, err := i.backend.Measurements(ctx)
	if err != nil {
		return &api.MetadataResponse{Error: err}
	}
	return &api.MetadataResponse{
		IssuerType: string(issuer.IssuerTypeTDX),
		Metadata:   measurements,
	}
}

// AttestationDocumentFromResponse unmarshals an IssueResponse document into an AttestationDocument.
func AttestationDocumentFromResponse(resp *api.IssueResponse) (*attestation.AttestationDocument, error) {
	var doc attestation.AttestationDocument
	if err := json.Unmarshal(resp.Document, &doc); err != nil {
		return nil, err
	}
	return &doc, nil
}
