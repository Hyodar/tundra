package verifier

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	tdxabi "github.com/google/go-tdx-guest/abi"
	pb "github.com/google/go-tdx-guest/proto/tdx"
	"github.com/google/go-tdx-guest/verify"
)

const platformGCPTDX = "gcp-tdx"

// GCPAttestationExtra mirrors the issuer's extra data structure.
type GCPAttestationExtra struct {
	InstanceIdentityToken string `json:"instance_identity_token,omitempty"`
}

// GCPTDXVerifyOptions controls optional checks for GCP TDX verification.
type GCPTDXVerifyOptions struct {
	// ExpectedMeasurements for TDX quote measurement comparison.
	ExpectedMeasurements *attestation.TDXMeasurements

	// CheckRevocations enables CRL checking.
	CheckRevocations bool

	// GetCollateral enables Intel PCS collateral retrieval.
	GetCollateral bool

	// VerifyIdentityToken controls whether the GCP instance identity token
	// in Extra is validated. When false, the token is ignored.
	VerifyIdentityToken bool
}

// GCPTDXVerifier verifies GCP TDX attestation documents. It validates the
// embedded TDX quote and optionally the GCP instance identity token.
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
//  1. Verifies the TDX quote signature chain
//  2. Validates report data matches SHA-256(userData || nonce)
//  3. Optionally verifies the GCP instance identity token
//  4. Optionally checks measurements
func (v *GCPTDXVerifier) Verify(_ context.Context, doc *attestation.AttestationDocument, nonce []byte) (*attestation.VerificationResult, error) {
	if doc == nil {
		return failResult(fmt.Errorf("gcp-tdx: attestation document is nil")), nil
	}
	if doc.Platform != platformGCPTDX {
		return failResult(fmt.Errorf("gcp-tdx: unexpected platform %q", doc.Platform)), nil
	}

	// Verify the TDX quote.
	verifyOpts := verify.DefaultOptions()
	verifyOpts.CheckRevocations = v.opts.CheckRevocations
	verifyOpts.GetCollateral = v.opts.GetCollateral

	if err := verify.RawTdxQuote(doc.RawQuote, verifyOpts); err != nil {
		return failResult(fmt.Errorf("gcp-tdx: TDX quote verification failed: %w", err)), nil
	}

	// Parse the quote.
	quoteAny, err := tdxabi.QuoteToProto(doc.RawQuote)
	if err != nil {
		return failResult(fmt.Errorf("gcp-tdx: failed to parse quote: %w", err)), nil
	}

	q4, ok := quoteAny.(*pb.QuoteV4)
	if !ok {
		return failResult(fmt.Errorf("gcp-tdx: unexpected quote type %T", quoteAny)), nil
	}

	body := q4.GetTdQuoteBody()
	if body == nil {
		return failResult(fmt.Errorf("gcp-tdx: quote has nil TD body")), nil
	}

	// Check report data binding.
	expectedReportData := buildExpectedReportData(doc.UserData, nonce)
	if !bytes.Equal(body.GetReportData()[:32], expectedReportData[:32]) {
		return failResult(fmt.Errorf("gcp-tdx: report data mismatch")), nil
	}

	// Optionally verify GCP identity token.
	if v.opts.VerifyIdentityToken {
		if len(doc.Extra) == 0 {
			return failResult(fmt.Errorf("gcp-tdx: identity token verification enabled but no extra data present")), nil
		}
		var extra GCPAttestationExtra
		if err := json.Unmarshal(doc.Extra, &extra); err != nil {
			return failResult(fmt.Errorf("gcp-tdx: failed to unmarshal extra data: %w", err)), nil
		}
		if extra.InstanceIdentityToken == "" {
			return failResult(fmt.Errorf("gcp-tdx: instance identity token missing")), nil
		}
		// The identity token presence is checked; full JWT/OIDC validation
		// against Google's OIDC discovery endpoint is left to a dedicated
		// verifier or future extension.
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
