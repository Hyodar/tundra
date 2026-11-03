package simulator

import (
	"context"
	"encoding/hex"
	"encoding/json"

	"github.com/Hyodar/tundra/tools/pkg/modes"

	tundraissuer "github.com/Hyodar/tundra/tools/pkg/attestation/issuer"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/issuer"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/logger"
)

type SimulatorIssuer struct {
	issuer.Issuer

	logger  logger.Logger
	backend tundraissuer.Issuer
	// events is the firewall's mode log: the simulated RTMR3 is its replay, as a TD's
	// would be the switcher's extends.
	events string
}

func NewSimulatorIssuer(logger logger.Logger) *SimulatorIssuer {
	return &SimulatorIssuer{
		backend: tundraissuer.NewSimulatorIssuer(),
		logger:  logger,
		events:  modes.EventsPath,
	}
}

func (i *SimulatorIssuer) Start(_ context.Context) error {
	return nil
}

func (i *SimulatorIssuer) Issue(ctx context.Context, req *api.IssueRequest) *api.IssueResponse {
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

func (i *SimulatorIssuer) Metadata(ctx context.Context, req *api.MetadataRequest) *api.MetadataResponse {
	measurements, err := i.backend.Measurements(ctx)
	if err != nil {
		return &api.MetadataResponse{Error: err}
	}
	events, err := modes.ReadEvents(i.events)
	if err != nil {
		return &api.MetadataResponse{Error: err}
	}
	simulated := *measurements
	simulated.Rtmr3 = hex.EncodeToString(modes.Replay(modes.Lines(events)))
	return &api.MetadataResponse{
		IssuerType: string(issuer.IssuerTypeSimulator),
		Metadata:   &simulated,
	}
}
