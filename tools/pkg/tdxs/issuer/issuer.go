package issuer

import (
	"context"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
)

type Issuer interface {
	Start(ctx context.Context) error
	Issue(ctx context.Context, req *api.IssueRequest) *api.IssueResponse
	Metadata(ctx context.Context, req *api.MetadataRequest) *api.MetadataResponse
}

type IssuerType string

const (
	IssuerTypeAzure     IssuerType = "azure"
	IssuerTypeGCP       IssuerType = "gcp"
	IssuerTypeSimulator IssuerType = "simulator"
	IssuerTypeTDX       IssuerType = "tdx"
)

const (
	MetadataUserData = "userData"
	MetadataNonce    = "nonce"
)
