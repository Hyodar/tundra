package api

type IssueRequest struct {
	UserData []byte
	Nonce    []byte
	Options  any
}

type MetadataRequest struct {
	Options any
}

type ValidateRequest struct {
	Document []byte
	Nonce    []byte
	Options  any
}
