package issuer

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

const platformSimulator = "simulator"

// simulatorSecret is a fixed key used by the simulator to produce a
// deterministic HMAC "signature" that the simulator verifier can check.
// This is NOT cryptographically secure -- it exists solely for testing.
var simulatorSecret = []byte("tundra-simulator-secret-key")

// SimulatorIssuer produces fake attestation documents for testing.
// The "quote" it generates is an HMAC-SHA256 over the report data,
// which the SimulatorVerifier can validate.
type SimulatorIssuer struct {
	measurements *attestation.TDXMeasurements
}

// SimulatorIssuerOption configures a SimulatorIssuer.
type SimulatorIssuerOption func(*SimulatorIssuer)

// WithSimulatorMeasurements sets the measurements returned by the simulator.
func WithSimulatorMeasurements(m *attestation.TDXMeasurements) SimulatorIssuerOption {
	return func(s *SimulatorIssuer) {
		s.measurements = m
	}
}

// NewSimulatorIssuer creates a SimulatorIssuer for testing purposes.
func NewSimulatorIssuer(opts ...SimulatorIssuerOption) *SimulatorIssuer {
	s := &SimulatorIssuer{
		measurements: defaultSimulatorMeasurements(),
	}
	for _, o := range opts {
		o(s)
	}
	return s
}

// Platform returns "simulator".
func (s *SimulatorIssuer) Platform() string { return platformSimulator }

// Issue creates a simulated attestation document. The RawQuote field contains
// a 32-byte report-data hash followed by a 32-byte HMAC tag.
func (s *SimulatorIssuer) Issue(_ context.Context, userData, nonce []byte) (*attestation.AttestationDocument, error) {
	reportData := buildReportData(userData, nonce)

	// The simulator "quote" is: reportData[:32] || HMAC(reportData[:32])
	mac := hmac.New(sha256.New, simulatorSecret)
	mac.Write(reportData[:32])
	tag := mac.Sum(nil)

	rawQuote := make([]byte, 0, 64)
	rawQuote = append(rawQuote, reportData[:32]...)
	rawQuote = append(rawQuote, tag...)

	return &attestation.AttestationDocument{
		RawQuote: rawQuote,
		UserData: userData,
		Nonce:    nonce,
		Platform: platformSimulator,
	}, nil
}

// Measurements returns the configured (or default) simulated measurements.
func (s *SimulatorIssuer) Measurements(_ context.Context) (*attestation.TDXMeasurements, error) {
	return s.measurements, nil
}

func defaultSimulatorMeasurements() *attestation.TDXMeasurements {
	return &attestation.TDXMeasurements{
		MrTd:    hex.EncodeToString(make([]byte, 48)),
		MrOwner: hex.EncodeToString(make([]byte, 48)),
		MrSeam:  hex.EncodeToString(make([]byte, 48)),
		Rtmr0:   hex.EncodeToString(make([]byte, 48)),
		Rtmr1:   hex.EncodeToString(make([]byte, 48)),
		Rtmr2:   hex.EncodeToString(make([]byte, 48)),
		Rtmr3:   hex.EncodeToString(make([]byte, 48)),
		XFAM:    hex.EncodeToString(make([]byte, 8)),
	}
}
