package verifier

import (
	"bytes"
	"context"
	"fmt"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

const platformAzureTDX = "azure-tdx"

// AzureTDXVerifyOptions controls optional checks for Azure TDX verification.
type AzureTDXVerifyOptions struct {
	// ExpectedMeasurements for TDX quote measurement comparison.
	ExpectedMeasurements *attestation.TDXMeasurements

	// CheckRevocations enables CRL checking.
	CheckRevocations bool

	// GetCollateral enables Intel PCS collateral retrieval.
	GetCollateral bool
}

// AzureTDXVerifier verifies Azure TDX attestation documents. It validates the
// embedded TDX quote alone: the IMDS attested document in Extra binds neither the nonce
// nor the quote, and its PKCS#7 chain to Microsoft is not checked, so it is ignored.
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
//  1. Refuses a DEBUG TD and verifies the TDX quote signature chain
//  2. Validates report data matches SHA-256(userData || nonce)
//  3. Optionally checks measurements
func (v *AzureTDXVerifier) Verify(_ context.Context, doc *attestation.AttestationDocument, nonce []byte) (*attestation.VerificationResult, error) {
	if doc == nil {
		return failResult(fmt.Errorf("azure-tdx: attestation document is nil")), nil
	}
	if doc.Platform != platformAzureTDX {
		return failResult(fmt.Errorf("azure-tdx: unexpected platform %q", doc.Platform)), nil
	}

	q4, err := verifiedQuote(platformAzureTDX, doc.RawQuote, v.opts.CheckRevocations, v.opts.GetCollateral)
	if err != nil {
		return failResult(err), nil
	}
	body := q4.GetTdQuoteBody()

	// Check report data binding.
	expectedReportData := buildExpectedReportData(doc.UserData, nonce)
	if !bytes.Equal(body.GetReportData()[:32], expectedReportData[:32]) {
		return failResult(fmt.Errorf("azure-tdx: report data mismatch")), nil
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
