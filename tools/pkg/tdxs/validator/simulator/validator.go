package simulator

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

type SimulatorValidator struct {
	validator.Validator

	logger  logger.Logger
	backend tundraverifier.Verifier
}

func NewSimulatorValidator(logger logger.Logger) *SimulatorValidator {
	return &SimulatorValidator{
		backend: tundraverifier.NewSimulatorVerifier(),
		logger:  logger,
	}
}

func (v *SimulatorValidator) Start(_ context.Context) error {
	return nil
}

func (v *SimulatorValidator) Validate(ctx context.Context, req *api.ValidateRequest) *api.ValidateResponse {
	if req.Document == nil {
		return &api.ValidateResponse{Error: fmt.Errorf("document is nil")}
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
