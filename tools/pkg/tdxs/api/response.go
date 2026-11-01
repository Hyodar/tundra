package api

type IssueResponse struct {
	Document []byte
	Error    error
}

type ValidateResponse struct {
	UserData []byte
	Valid    bool
	Error    error
}

type MetadataResponse struct {
	IssuerType string
	UserData   []byte
	Nonce      []byte
	Metadata   any
	Error      error
}
