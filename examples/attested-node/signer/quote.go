package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"time"
)

// evidence asks the image's evidence service (tdxs, on a unix socket) for quotes whose
// report data is the signer's compressed public key, so a client can check that the key
// signing its responses lives in the TD the quote attests.
type evidence struct {
	socket string
	signer []byte
}

type issueRequest struct {
	Method string `json:"method"`
	Data   struct {
		UserData string `json:"userData"`
		Nonce    string `json:"nonce"`
	} `json:"data"`
}

type issueResponse struct {
	Data *struct {
		Document string `json:"document"`
	} `json:"data"`
	Error *string `json:"error"`
}

// issue returns the attestation document tdxs issues over the signer's key and nonce.
func (e *evidence) issue(nonce []byte) (json.RawMessage, error) {
	conn, err := net.DialTimeout("unix", e.socket, 10*time.Second)
	if err != nil {
		return nil, fmt.Errorf("the evidence service at %s: %w", e.socket, err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(60 * time.Second))
	var request issueRequest
	request.Method = "issue"
	request.Data.UserData = hex.EncodeToString(e.signer)
	request.Data.Nonce = hex.EncodeToString(nonce)
	if err := json.NewEncoder(conn).Encode(request); err != nil {
		return nil, fmt.Errorf("ask %s for a quote: %w", e.socket, err)
	}
	var response issueResponse
	if err := json.NewDecoder(conn).Decode(&response); err != nil {
		return nil, fmt.Errorf("read the quote from %s: %w", e.socket, err)
	}
	if response.Error != nil {
		return nil, fmt.Errorf("the evidence service refused a quote: %s", *response.Error)
	}
	if response.Data == nil {
		return nil, errors.New("the evidence service answered no document")
	}
	document, err := hex.DecodeString(response.Data.Document)
	if err != nil || !json.Valid(document) {
		return nil, errors.New("the evidence service answered a document that is not hex JSON")
	}
	return document, nil
}

// check issues one quote over a fresh nonce: the signer refuses to start in a TD whose
// evidence service cannot attest its key.
func (e *evidence) check() error {
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	_, err := e.issue(nonce)
	return err
}
