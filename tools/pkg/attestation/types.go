// Package attestation provides types and interfaces for TDX attestation
// issuance and verification across raw TDX, Azure TDX, and GCP TDX platforms.
package attestation

// AttestationDocument holds the attestation data produced by an issuer
// and consumed by a verifier.
type AttestationDocument struct {
	RawQuote []byte `json:"raw_quote"`
	UserData []byte `json:"user_data"`
	Nonce    []byte `json:"nonce,omitempty"`
	Platform string `json:"platform"` // "tdx", "azure-tdx", "gcp-tdx"
	Extra    []byte `json:"extra,omitempty"`
}

// TDXMeasurements contains the TDX-specific measurement registers
// extracted from a TDX quote.
type TDXMeasurements struct {
	MrTd    string `json:"mrtd"`
	MrOwner string `json:"mrowner"`
	MrSeam  string `json:"mrseam"`
	Rtmr0   string `json:"rtmr0"`
	Rtmr1   string `json:"rtmr1"`
	Rtmr2   string `json:"rtmr2"`
	Rtmr3   string `json:"rtmr3"`
	XFAM    string `json:"xfam"`
}

// VerificationResult holds the outcome of verifying an attestation document.
type VerificationResult struct {
	Valid        bool             `json:"valid"`
	UserData     []byte           `json:"user_data"`
	Measurements *TDXMeasurements `json:"measurements,omitempty"`
	Error        error            `json:"error,omitempty"`
}
