// Command sink receives what an Observability export sends, on the loopback: journal
// uploads (systemd-journal-upload POSTs /upload) and Prometheus remote writes. It answers
// each POST with 2xx and prints "sink: <kind> batch <n> <bytes> bytes" to /dev/console,
// not to the journal, whose upload would otherwise feed itself: one line per remote-write
// POST, and for a journal upload, which streams for as long as the uploader follows the
// journal, one line for its first data and then at most one every two seconds.
//
// A remote write's series names are decoded, and the first of each name prints
// "sink: remote-write series <name>"; GET /api/v1/label/__name__/values answers them as
// Prometheus does, so the boot probe asks this sink what reached it as it asks metrics-sink.
//
// It is the export's proxy too: a request in absolute form (what an exporter sends through
// Observability(proxy=) to an http:// sink) is forwarded to its host, and the first one to
// each host prints "sink: proxy carries <host>".
//
// Usage: sink ADDRESS...
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"slices"
	"sync"
	"syscall"
	"time"
)

type counter struct {
	mu      sync.Mutex
	batches map[string]int
	carried map[string]bool
	names   map[string]bool
	out     io.Writer
}

var direct = &http.Transport{}

func (c *counter) forward(w http.ResponseWriter, r *http.Request) {
	c.mu.Lock()
	if !c.carried[r.URL.Host] {
		c.carried[r.URL.Host] = true
		fmt.Fprintf(c.out, "sink: proxy carries %s\n", r.URL.Host)
	}
	c.mu.Unlock()
	out := r.Clone(r.Context())
	out.RequestURI = ""
	out.Header.Del("Proxy-Connection")
	response, err := direct.RoundTrip(out)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer response.Body.Close()
	for key, values := range response.Header {
		w.Header()[key] = values
	}
	w.WriteHeader(response.StatusCode)
	io.Copy(w, response.Body)
}

func (c *counter) mark(kind string, size int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.batches[kind]++
	fmt.Fprintf(c.out, "sink: %s batch %d %d bytes\n", kind, c.batches[kind], size)
}

func (c *counter) stream(kind string, body io.Reader) error {
	buf := make([]byte, 64<<10)
	var pending int64
	var last time.Time
	for {
		n, err := body.Read(buf)
		pending += int64(n)
		if pending > 0 && (err != nil || time.Since(last) >= 2*time.Second) {
			c.mark(kind, pending)
			pending, last = 0, time.Now()
		}
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

func (c *counter) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.IsAbs() {
		c.forward(w, r)
		return
	}
	if r.Method == http.MethodGet && r.URL.Path == "/api/v1/label/__name__/values" {
		c.mu.Lock()
		names := make([]string, 0, len(c.names))
		for name := range c.names {
			names = append(names, name)
		}
		c.mu.Unlock()
		slices.Sort(names)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"status": "success", "data": names})
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	if r.URL.Path == "/upload" {
		if err := c.stream("journal", r.Body); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		w.WriteHeader(http.StatusAccepted)
		return
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	names, err := seriesNames(body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	c.mark("remote-write", int64(len(body)))
	c.mu.Lock()
	for _, name := range names {
		if !c.names[name] {
			c.names[name] = true
			fmt.Fprintf(c.out, "sink: remote-write series %s\n", name)
		}
	}
	c.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

func main() {
	if len(os.Args) < 2 {
		log.Fatal("usage: sink ADDRESS...")
	}
	var out io.Writer = os.Stdout
	if console, err := os.OpenFile("/dev/console", os.O_WRONLY|syscall.O_NOCTTY, 0); err == nil {
		out = console
	}
	handler := &counter{batches: map[string]int{}, carried: map[string]bool{}, names: map[string]bool{}, out: out}
	failed := make(chan error, len(os.Args)-1)
	for _, address := range os.Args[1:] {
		go func(address string) { failed <- http.ListenAndServe(address, handler) }(address)
	}
	log.Fatal(<-failed)
}
