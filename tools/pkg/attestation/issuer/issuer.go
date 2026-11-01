// Package issuer provides interfaces and implementations for producing
// TDX attestation documents on various platforms.
package issuer

import (
	"context"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

// Issuer produces attestation documents from a TDX-capable platform.
type Issuer interface {
	// Issue creates an attestation document binding the given userData and nonce.
	// The report data embedded in the TDX quote is SHA-256(userData || nonce).
	Issue(ctx context.Context, userData, nonce []byte) (*attestation.AttestationDocument, error)

	// Measurements reads the current TDX measurements from the platform.
	Measurements(ctx context.Context) (*attestation.TDXMeasurements, error)

	// Platform returns the platform identifier (e.g. "tdx", "azure-tdx", "gcp-tdx").
	Platform() string
}
