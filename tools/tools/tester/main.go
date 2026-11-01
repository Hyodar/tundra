package main

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
)

type SocketTransportRequest struct {
	Method string          `json:"type"`
	Data   json.RawMessage `json:"data"`
}

type SocketTransportIssueRequest struct {
	UserData string `json:"userData"`
	Nonce    string `json:"nonce"`
}

type SocketTransportIssueResponse struct {
	Document string `json:"document"`
	Error    string `json:"error"`
}

func main() {
	var (
		socketPath = flag.String("socket", "./tdxs.sock", "Path to the Unix socket")
		userDataLen = flag.Int("userdata-len", 32, "Length of random user data in bytes")
		nonceLen = flag.Int("nonce-len", 32, "Length of random nonce in bytes")
	)
	flag.Parse()

	// Generate random user data and nonce
	userData := make([]byte, *userDataLen)
	nonce := make([]byte, *nonceLen)
	
	if _, err := rand.Read(userData); err != nil {
		fmt.Fprintf(os.Stderr, "Error generating random user data: %v\n", err)
		os.Exit(1)
	}
	
	if _, err := rand.Read(nonce); err != nil {
		fmt.Fprintf(os.Stderr, "Error generating random nonce: %v\n", err)
		os.Exit(1)
	}

	// Create issue request
	issueReq := SocketTransportIssueRequest{
		UserData: hex.EncodeToString(userData),
		Nonce:    hex.EncodeToString(nonce),
	}

	issueReqData, err := json.Marshal(issueReq)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error marshaling issue request: %v\n", err)
		os.Exit(1)
	}

	// Create transport request
	transportReq := SocketTransportRequest{
		Method: "issue",
		Data:   json.RawMessage(issueReqData),
	}

	reqData, err := json.Marshal(transportReq)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error marshaling transport request: %v\n", err)
		os.Exit(1)
	}

	// Connect to Unix socket
	conn, err := net.Dial("unix", *socketPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error connecting to socket %s: %v\n", *socketPath, err)
		os.Exit(1)
	}
	defer conn.Close()

	fmt.Printf("Connected to socket: %s\n", *socketPath)
	fmt.Printf("Sending issue request:\n")
	fmt.Printf("  UserData (hex): %s\n", hex.EncodeToString(userData))
	fmt.Printf("  Nonce (hex):    %s\n", hex.EncodeToString(nonce))
	fmt.Println()

	// Send request
	if _, err := conn.Write(reqData); err != nil {
		fmt.Fprintf(os.Stderr, "Error sending request: %v\n", err)
		os.Exit(1)
	}

	// Read response
	var respBuf bytes.Buffer
	buf := make([]byte, 4096)
	for {
		n, err := conn.Read(buf)
		if err != nil {
			if err != io.EOF {
				fmt.Fprintf(os.Stderr, "Error reading response: %v\n", err)
				os.Exit(1)
			}
			break
		}
		respBuf.Write(buf[:n])
		
		// Try to parse the response to see if we have a complete JSON object
		var resp SocketTransportIssueResponse
		if err := json.Unmarshal(respBuf.Bytes(), &resp); err == nil {
			// We have a complete response
			break
		}
	}

	// Parse and display response
	var response SocketTransportIssueResponse
	if err := json.Unmarshal(respBuf.Bytes(), &response); err != nil {
		fmt.Fprintf(os.Stderr, "Error parsing response: %v\n", err)
		fmt.Fprintf(os.Stderr, "Raw response: %s\n", respBuf.String())
		os.Exit(1)
	}

	fmt.Println("Received response:")
	if response.Error != "" {
		fmt.Printf("  Error: %s\n", response.Error)
	} else {
		fmt.Printf("  Document (hex): %s\n", response.Document)
		
		// Try to decode and display the document
		if docBytes, err := hex.DecodeString(response.Document); err == nil {
			fmt.Printf("  Document size: %d bytes\n", len(docBytes))
			
			// If it looks like JSON, try to pretty print it
			var jsonDoc interface{}
			if err := json.Unmarshal(docBytes, &jsonDoc); err == nil {
				if prettyJSON, err := json.MarshalIndent(jsonDoc, "  ", "  "); err == nil {
					fmt.Println("  Document (JSON):")
					fmt.Printf("  %s\n", string(prettyJSON))
				}
			}
		}
	}
}