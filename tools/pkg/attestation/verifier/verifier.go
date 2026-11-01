// Package verifier provides interfaces and implementations for verifying
// TDX attestation documents on various platforms.
package verifier

import (
	"context"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

// Verifier validates attestation documents produced by an Issuer.
type Verifier interface {
	// Verify checks the attestation document's validity, including signature
	// verification, report data binding, and optional measurement checks.
	Verify(ctx context.Context, doc *attestation.AttestationDocument, nonce []byte) (*attestation.VerificationResult, error)

	// Platform returns the platform identifier this verifier handles.
	Platform() string
}
