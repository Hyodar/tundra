package gcp

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	tundraverifier "github.com/Hyodar/tundra/tools/pkg/attestation/verifier"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/logger"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/validator"
)

type GCPValidator struct {
	validator.Validator

	logger  logger.Logger
	backend tundraverifier.Verifier
}

type GCPValidatorConfig struct {
	ExpectedMeasurements *attestation.TDXMeasurements `yaml:"expected_measurements,omitempty"`
	CheckRevocations     bool                         `yaml:"check_revocations,omitempty"`
	GetCollateral        bool                         `yaml:"get_collateral,omitempty"`
}

func NewGCPValidator(cfg *GCPValidatorConfig, logger logger.Logger) *GCPValidator {
	opts := tundraverifier.GCPTDXVerifyOptions{}
	if cfg != nil {
		opts.ExpectedMeasurements = cfg.ExpectedMeasurements
		opts.CheckRevocations = cfg.CheckRevocations
		opts.GetCollateral = cfg.GetCollateral
	}
	return &GCPValidator{
		backend: tundraverifier.NewGCPTDXVerifier(opts),
		logger:  logger,
	}
}

func (v *GCPValidator) Start(_ context.Context) error {
	return nil
}

func (v *GCPValidator) Validate(ctx context.Context, req *api.ValidateRequest) *api.ValidateResponse {
	if v.backend == nil {
		return &api.ValidateResponse{Error: fmt.Errorf("backend not initialized")}
	}

	var doc attestation.AttestationDocument
	if err := json.Unmarshal(req.Document, &doc); err != nil {
		return &api.ValidateResponse{Error: fmt.Errorf("unmarshal attestation document: %w", err)}
	}

	result, err := v.backend.Verify(ctx, &doc, req.Nonce)
	if err != nil {
		return &api.ValidateResponse{Error: err}
	}
	if !result.Valid {
		return &api.ValidateResponse{Error: result.Error}
	}
	return &api.ValidateResponse{UserData: doc.UserData, Valid: true}
}
