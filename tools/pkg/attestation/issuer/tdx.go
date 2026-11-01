package issuer

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	tdxabi "github.com/google/go-tdx-guest/abi"
	tdxclient "github.com/google/go-tdx-guest/client"
	pb "github.com/google/go-tdx-guest/proto/tdx"
)

const platformTDX = "tdx"

// QuoteProvider abstracts over the go-tdx-guest QuoteProvider for testability.
type QuoteProvider interface {
	IsSupported() error
	GetRawQuote(reportData [64]byte) ([]uint8, error)
}

// TDXIssuer produces raw TDX attestation documents using the configfs-tsm
// interface (via go-tdx-guest's LinuxConfigFsQuoteProvider).
type TDXIssuer struct {
	provider QuoteProvider
}

// TDXIssuerOption configures TDXIssuer construction.
type TDXIssuerOption func(*TDXIssuer)

// WithQuoteProvider overrides the default quote provider, primarily for testing.
func WithQuoteProvider(p QuoteProvider) TDXIssuerOption {
	return func(i *TDXIssuer) {
		i.provider = p
	}
}

// NewTDXIssuer creates a TDXIssuer. When no QuoteProvider option is supplied
// it attempts to obtain a LinuxConfigFsQuoteProvider from go-tdx-guest.
func NewTDXIssuer(opts ...TDXIssuerOption) (*TDXIssuer, error) {
	issuer := &TDXIssuer{}
	for _, o := range opts {
		o(issuer)
	}
	if issuer.provider == nil {
		p, err := tdxclient.GetQuoteProvider()
		if err != nil {
			return nil, fmt.Errorf("tdx: failed to get quote provider: %w", err)
		}
		issuer.provider = p
	}
	return issuer, nil
}

// Platform returns "tdx".
func (t *TDXIssuer) Platform() string { return platformTDX }

// Issue produces an AttestationDocument by requesting a TDX quote whose
// report data is SHA-256(userData || nonce) zero-padded to 64 bytes.
func (t *TDXIssuer) Issue(_ context.Context, userData, nonce []byte) (*attestation.AttestationDocument, error) {
	reportData := buildReportData(userData, nonce)

	rawQuote, err := tdxclient.GetRawQuote(t.provider, reportData)
	if err != nil {
		return nil, fmt.Errorf("tdx: failed to get raw quote: %w", err)
	}

	return &attestation.AttestationDocument{
		RawQuote: rawQuote,
		UserData: userData,
		Nonce:    nonce,
		Platform: platformTDX,
	}, nil
}

// Measurements requests a TDX quote with zeroed report data and extracts the
// measurement registers from the TD quote body.
func (t *TDXIssuer) Measurements(_ context.Context) (*attestation.TDXMeasurements, error) {
	var reportData [64]byte
	rawQuote, err := tdxclient.GetRawQuote(t.provider, reportData)
	if err != nil {
		return nil, fmt.Errorf("tdx: failed to get quote for measurements: %w", err)
	}

	quoteAny, err := tdxabi.QuoteToProto(rawQuote)
	if err != nil {
		return nil, fmt.Errorf("tdx: failed to parse quote: %w", err)
	}

	q4, ok := quoteAny.(*pb.QuoteV4)
	if !ok {
		return nil, fmt.Errorf("tdx: unexpected quote type %T", quoteAny)
	}

	return extractMeasurements(q4), nil
}

// buildReportData computes SHA-256(userData || nonce) and places the 32-byte
// hash into the first half of a 64-byte array.
func buildReportData(userData, nonce []byte) [64]byte {
	h := sha256.New()
	h.Write(userData)
	h.Write(nonce)
	digest := h.Sum(nil)

	var rd [64]byte
	copy(rd[:], digest)
	return rd
}

// extractMeasurements pulls measurement register values from a QuoteV4.
func extractMeasurements(q *pb.QuoteV4) *attestation.TDXMeasurements {
	body := q.GetTdQuoteBody()
	if body == nil {
		return nil
	}

	m := &attestation.TDXMeasurements{
		MrTd:    hex.EncodeToString(body.GetMrTd()),
		MrOwner: hex.EncodeToString(body.GetMrOwner()),
		MrSeam:  hex.EncodeToString(body.GetMrSeam()),
		XFAM:    hex.EncodeToString(body.GetXfam()),
	}

	rtmrs := body.GetRtmrs()
	if len(rtmrs) > 0 {
		m.Rtmr0 = hex.EncodeToString(rtmrs[0])
	}
	if len(rtmrs) > 1 {
		m.Rtmr1 = hex.EncodeToString(rtmrs[1])
	}
	if len(rtmrs) > 2 {
		m.Rtmr2 = hex.EncodeToString(rtmrs[2])
	}
	if len(rtmrs) > 3 {
		m.Rtmr3 = hex.EncodeToString(rtmrs[3])
	}

	return m
}
