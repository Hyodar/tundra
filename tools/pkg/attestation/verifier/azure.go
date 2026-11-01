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

const platformAzureTDX = "azure-tdx"

// AzureAttestationExtra mirrors the issuer's extra data structure.
type AzureAttestationExtra struct {
	IMDSAttestation json.RawMessage `json:"imds_attestation,omitempty"`
}

// AzureTDXVerifyOptions controls optional checks for Azure TDX verification.
type AzureTDXVerifyOptions struct {
	// ExpectedMeasurements for TDX quote measurement comparison.
	ExpectedMeasurements *attestation.TDXMeasurements

	// CheckRevocations enables CRL checking.
	CheckRevocations bool

	// GetCollateral enables Intel PCS collateral retrieval.
	GetCollateral bool

	// VerifyIMDS controls whether the IMDS attestation data in Extra is
	// validated. When false, the IMDS data is ignored.
	VerifyIMDS bool
}

// AzureTDXVerifier verifies Azure TDX attestation documents. It validates the
// embedded TDX quote and optionally the Azure IMDS attestation token.
type AzureTDXVerifier struct {
	opts AzureTDXVerifyOptions
}

// NewAzureTDXVerifier creates an AzureTDXVerifier with the given options.
func NewAzureTDXVerifier(opts AzureTDXVerifyOptions) *AzureTDXVerifier {
	return &AzureTDXVerifier{opts: opts}
}

// Platform returns "azure-tdx".
func (v *AzureTDXVerifier) Platform() string { return platformAzureTDX }

// Verify performs Azure TDX attestation verification:
//  1. Verifies the TDX quote signature chain
//  2. Validates report data matches SHA-256(userData || nonce)
//  3. Optionally verifies IMDS attestation data
//  4. Optionally checks measurements
func (v *AzureTDXVerifier) Verify(_ context.Context, doc *attestation.AttestationDocument, nonce []byte) (*attestation.VerificationResult, error) {
	if doc == nil {
		return failResult(fmt.Errorf("azure-tdx: attestation document is nil")), nil
	}
	if doc.Platform != platformAzureTDX {
		return failResult(fmt.Errorf("azure-tdx: unexpected platform %q", doc.Platform)), nil
	}

	// Verify the TDX quote.
	verifyOpts := verify.DefaultOptions()
	verifyOpts.CheckRevocations = v.opts.CheckRevocations
	verifyOpts.GetCollateral = v.opts.GetCollateral

	if err := verify.RawTdxQuote(doc.RawQuote, verifyOpts); err != nil {
		return failResult(fmt.Errorf("azure-tdx: TDX quote verification failed: %w", err)), nil
	}

	// Parse the quote.
	quoteAny, err := tdxabi.QuoteToProto(doc.RawQuote)
	if err != nil {
		return failResult(fmt.Errorf("azure-tdx: failed to parse quote: %w", err)), nil
	}

	q4, ok := quoteAny.(*pb.QuoteV4)
	if !ok {
		return failResult(fmt.Errorf("azure-tdx: unexpected quote type %T", quoteAny)), nil
	}

	body := q4.GetTdQuoteBody()
	if body == nil {
		return failResult(fmt.Errorf("azure-tdx: quote has nil TD body")), nil
	}

	// Check report data binding.
	expectedReportData := buildExpectedReportData(doc.UserData, nonce)
	if !bytes.Equal(body.GetReportData()[:32], expectedReportData[:32]) {
		return failResult(fmt.Errorf("azure-tdx: report data mismatch")), nil
	}

	// Optionally verify IMDS attestation.
	if v.opts.VerifyIMDS {
		if len(doc.Extra) == 0 {
			return failResult(fmt.Errorf("azure-tdx: IMDS verification enabled but no extra data present")), nil
		}
		var extra AzureAttestationExtra
		if err := json.Unmarshal(doc.Extra, &extra); err != nil {
			return failResult(fmt.Errorf("azure-tdx: failed to unmarshal extra data: %w", err)), nil
		}
		if len(extra.IMDSAttestation) == 0 {
			return failResult(fmt.Errorf("azure-tdx: IMDS attestation data missing")), nil
		}
		// The IMDS token presence is checked; full JWS validation requires
		// Azure-specific root certificates and is left to a dedicated MAA
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
