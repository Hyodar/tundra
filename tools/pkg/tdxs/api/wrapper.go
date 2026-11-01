package api

type IssueRequestWrapper struct {
	Request  *IssueRequest
	Response chan *IssueResponse
}

type MetadataRequestWrapper struct {
	Request  *MetadataRequest
	Response chan *MetadataResponse
}

type ValidateRequestWrapper struct {
	Request  *ValidateRequest
	Response chan *ValidateResponse
}
