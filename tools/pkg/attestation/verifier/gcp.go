package verifier

import (
	"bytes"
	"context"
	"fmt"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

const platformGCPTDX = "gcp-tdx"

// GCPTDXVerifyOptions controls optional checks for GCP TDX verification.
type GCPTDXVerifyOptions struct {
	// ExpectedMeasurements for TDX quote measurement comparison.
	ExpectedMeasurements *attestation.TDXMeasurements

	// CheckRevocations enables CRL checking.
	CheckRevocations bool

	// GetCollateral enables Intel PCS collateral retrieval.
	GetCollateral bool
}

// GCPTDXVerifier verifies GCP TDX attestation documents. It validates the
// embedded TDX quote alone: the instance identity token in Extra binds neither the nonce
// nor the quote (its audience is fixed), so it is ignored.
type GCPTDXVerifier struct {
	opts GCPTDXVerifyOptions
}

// NewGCPTDXVerifier creates a GCPTDXVerifier with the given options.
func NewGCPTDXVerifier(opts GCPTDXVerifyOptions) *GCPTDXVerifier {
	return &GCPTDXVerifier{opts: opts}
}

// Platform returns "gcp-tdx".
func (v *GCPTDXVerifier) Platform() string { return platformGCPTDX }

// Verify performs GCP TDX attestation verification:
//  1. Refuses a DEBUG TD and verifies the TDX quote signature chain
//  2. Validates report data matches SHA-256(userData || nonce)
//  3. Optionally checks measurements
func (v *GCPTDXVerifier) Verify(_ context.Context, doc *attestation.AttestationDocument, nonce []byte) (*attestation.VerificationResult, error) {
	if doc == nil {
		return failResult(fmt.Errorf("gcp-tdx: attestation document is nil")), nil
	}
	if doc.Platform != platformGCPTDX {
		return failResult(fmt.Errorf("gcp-tdx: unexpected platform %q", doc.Platform)), nil
	}

	q4, err := verifiedQuote(platformGCPTDX, doc.RawQuote, v.opts.CheckRevocations, v.opts.GetCollateral)
	if err != nil {
		return failResult(err), nil
	}
	body := q4.GetTdQuoteBody()

	// Check report data binding.
	expectedReportData := buildExpectedReportData(doc.UserData, nonce)
	if !bytes.Equal(body.GetReportData()[:32], expectedReportData[:32]) {
		return failResult(fmt.Errorf("gcp-tdx: report data mismatch")), nil
	}

	measurements := extractMeasurements(q4)

	if v.opts.ExpectedMeasurements != nil {
		if err := compareMeasurements(measurements, v.opts.ExpectedMeasurements); err != nil {
			return failResult(err), nil
		}
	}

	return &attestation.VerificationResult{
		Valid:        true,
		UserData:     doc.UserData,
		Measurements: measurements,
	}, nil
}
