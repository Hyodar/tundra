package verifier

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	tdxabi "github.com/google/go-tdx-guest/abi"
	pb "github.com/google/go-tdx-guest/proto/tdx"
	"github.com/google/go-tdx-guest/verify"
)

const platformTDX = "tdx"

// TDXVerifyOptions controls optional checks for TDX quote verification.
type TDXVerifyOptions struct {
	// ExpectedMeasurements, if non-nil, triggers comparison of the quote's
	// measurement registers against these expected values. Only non-empty
	// fields in ExpectedMeasurements are checked.
	ExpectedMeasurements *attestation.TDXMeasurements

	// CheckRevocations enables CRL checking against Intel's revocation list.
	CheckRevocations bool

	// GetCollateral enables retrieval and verification of collateral from
	// Intel's Provisioning Certification Service (PCS).
	GetCollateral bool
}

// TDXVerifier verifies raw TDX attestation documents using the
// go-tdx-guest verify library for cryptographic validation.
type TDXVerifier struct {
	opts TDXVerifyOptions
}

// NewTDXVerifier creates a TDXVerifier with the given options.
func NewTDXVerifier(opts TDXVerifyOptions) *TDXVerifier {
	return &TDXVerifier{opts: opts}
}

// Platform returns "tdx".
func (v *TDXVerifier) Platform() string { return platformTDX }

// Verify performs full TDX quote verification:
//  1. Parses the raw quote into a QuoteV4 protobuf and refuses a DEBUG TD
//  2. Verifies the signature chain via go-tdx-guest/verify
//  3. Checks that ReportData matches SHA-256(userData || nonce)
//  4. Optionally checks measurements against expected values
func (v *TDXVerifier) Verify(_ context.Context, doc *attestation.AttestationDocument, nonce []byte) (*attestation.VerificationResult, error) {
	if doc == nil {
		return failResult(fmt.Errorf("tdx: attestation document is nil")), nil
	}
	if doc.Platform != platformTDX {
		return failResult(fmt.Errorf("tdx: unexpected platform %q", doc.Platform)), nil
	}

	q4, err := verifiedQuote(platformTDX, doc.RawQuote, v.opts.CheckRevocations, v.opts.GetCollateral)
	if err != nil {
		return failResult(err), nil
	}
	body := q4.GetTdQuoteBody()

	// Step 3: Check report data binding.
	expectedReportData := buildExpectedReportData(doc.UserData, nonce)
	if !bytes.Equal(body.GetReportData()[:32], expectedReportData[:32]) {
		return failResult(fmt.Errorf("tdx: report data mismatch")), nil
	}

	// Step 4: Extract measurements.
	measurements := extractMeasurements(q4)

	// Step 5: Optionally verify measurements.
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

// tdDebug is bit 0 of a TD's ATTRIBUTES, TUD.DEBUG in the Intel TDX Module ABI: the host
// can read and write a debug TD's memory and registers, so nothing it quotes holds.
const tdDebug = 1

// verifiedQuote parses raw as a DCAP v4 TDX quote, refuses a TD with DEBUG set, then
// checks its signature chain to Intel's root.
func verifiedQuote(platform string, raw []byte, checkRevocations, getCollateral bool) (*pb.QuoteV4, error) {
	quoteAny, err := tdxabi.QuoteToProto(raw)
	if err != nil {
		return nil, fmt.Errorf("%s: failed to parse quote: %w", platform, err)
	}
	q4, ok := quoteAny.(*pb.QuoteV4)
	if !ok {
		return nil, fmt.Errorf("%s: unexpected quote type %T", platform, quoteAny)
	}
	body := q4.GetTdQuoteBody()
	if body == nil {
		return nil, fmt.Errorf("%s: quote has nil TD body", platform)
	}
	if attributes := body.GetTdAttributes(); len(attributes) == 0 || attributes[0]&tdDebug != 0 {
		return nil, fmt.Errorf("%s: the TD's ATTRIBUTES set TUD.DEBUG (bit 0): the host can read and change "+
			"a debug TD, so its quote proves nothing\nfix: launch the TD without debug", platform)
	}
	verifyOpts := verify.DefaultOptions()
	verifyOpts.CheckRevocations = checkRevocations
	verifyOpts.GetCollateral = getCollateral
	if err := verify.RawTdxQuote(raw, verifyOpts); err != nil {
		return nil, fmt.Errorf("%s: quote verification failed: %w", platform, err)
	}
	return q4, nil
}

// buildExpectedReportData computes SHA-256(userData || nonce).
func buildExpectedReportData(userData, nonce []byte) [32]byte {
	h := sha256.New()
	h.Write(userData)
	h.Write(nonce)
	var out [32]byte
	copy(out[:], h.Sum(nil))
	return out
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

// compareMeasurements checks that actual measurements match expected values.
// Only non-empty expected fields are compared.
func compareMeasurements(actual, expected *attestation.TDXMeasurements) error {
	if actual == nil {
		return fmt.Errorf("tdx: actual measurements are nil")
	}
	checks := []struct {
		name     string
		actual   string
		expected string
	}{
		{"mrtd", actual.MrTd, expected.MrTd},
		{"mrowner", actual.MrOwner, expected.MrOwner},
		{"mrseam", actual.MrSeam, expected.MrSeam},
		{"rtmr0", actual.Rtmr0, expected.Rtmr0},
		{"rtmr1", actual.Rtmr1, expected.Rtmr1},
		{"rtmr2", actual.Rtmr2, expected.Rtmr2},
		{"rtmr3", actual.Rtmr3, expected.Rtmr3},
		{"xfam", actual.XFAM, expected.XFAM},
	}
	for _, c := range checks {
		if c.expected != "" && c.actual != c.expected {
			return fmt.Errorf("tdx: measurement %s mismatch: got %s, want %s", c.name, c.actual, c.expected)
		}
	}
	return nil
}

// failResult is a helper to build a failed VerificationResult.
func failResult(err error) *attestation.VerificationResult {
	return &attestation.VerificationResult{
		Valid: false,
		Error: err,
	}
}
