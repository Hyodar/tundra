package verifier

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"fmt"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

const platformSimulator = "simulator"

// simulatorSecret must match the key used by the simulator issuer.
var simulatorSecret = []byte("tundra-simulator-secret-key")

// SimulatorVerifier validates attestation documents produced by the
// SimulatorIssuer. It checks the HMAC tag and report data binding.
type SimulatorVerifier struct{}

// NewSimulatorVerifier creates a SimulatorVerifier.
func NewSimulatorVerifier() *SimulatorVerifier {
	return &SimulatorVerifier{}
}

// Platform returns "simulator".
func (v *SimulatorVerifier) Platform() string { return platformSimulator }

// Verify validates a simulator attestation document:
//  1. Checks the platform is "simulator"
//  2. Verifies the RawQuote length is exactly 64 bytes (32 hash + 32 HMAC)
//  3. Recomputes the expected report data hash and HMAC tag
//  4. Returns a successful result with zero-valued measurements
func (v *SimulatorVerifier) Verify(_ context.Context, doc *attestation.AttestationDocument, nonce []byte) (*attestation.VerificationResult, error) {
	if doc == nil {
		return failResult(fmt.Errorf("simulator: attestation document is nil")), nil
	}
	if doc.Platform != platformSimulator {
		return failResult(fmt.Errorf("simulator: unexpected platform %q", doc.Platform)), nil
	}
	if len(doc.RawQuote) != 64 {
		return failResult(fmt.Errorf("simulator: invalid quote length %d, expected 64", len(doc.RawQuote))), nil
	}

	// Recompute the expected report data hash.
	expectedReportData := buildExpectedReportData(doc.UserData, nonce)

	// The first 32 bytes of the quote are the report data hash.
	quoteHash := doc.RawQuote[:32]
	quoteTag := doc.RawQuote[32:64]

	if !hmac.Equal(quoteHash, expectedReportData[:]) {
		return failResult(fmt.Errorf("simulator: report data mismatch")), nil
	}

	// Verify the HMAC tag.
	mac := hmac.New(sha256.New, simulatorSecret)
	mac.Write(quoteHash)
	expectedTag := mac.Sum(nil)

	if !hmac.Equal(quoteTag, expectedTag) {
		return failResult(fmt.Errorf("simulator: HMAC verification failed")), nil
	}

	return &attestation.VerificationResult{
		Valid:    true,
		UserData: doc.UserData,
	}, nil
}
