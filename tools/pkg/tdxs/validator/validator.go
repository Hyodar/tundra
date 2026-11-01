package validator

import (
	"context"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
)

type Validator interface {
	Start(ctx context.Context) error
	Validate(ctx context.Context, req *api.ValidateRequest) *api.ValidateResponse
}

type ValidatorType string

const (
	ValidatorTypeAzure     ValidatorType = "azure"
	ValidatorTypeGCP       ValidatorType = "gcp"
	ValidatorTypeSimulator ValidatorType = "simulator"
	ValidatorTypeTDX       ValidatorType = "tdx"
)
