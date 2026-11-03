package api

type IssueRequest struct {
	// UserData reaches the issuer under the caller's tag (attestation.Tagged).
	UserData []byte
	// Caller is the tag the request claims; empty claims none.
	Caller  string
	Nonce   []byte
	Options any
}

type MetadataRequest struct {
	Options any
}

type ValidateRequest struct {
	Document []byte
	Nonce    []byte
	Options  any
}
