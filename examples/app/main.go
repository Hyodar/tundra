// The program examples 04 and 06 build from this directory (Local("app")): it answers
// on -listen until stopped, and `app index` keeps its cache until stopped.
package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:8080", "the address to answer on")
	flag.String("token-file", "", "the delivered API token")
	flag.String("flags-file", "", "the delivered feature flags")
	flag.String("tdxs-socket", "/var/tdxs.sock", "the evidence service's socket")
	flag.Parse()
	if flag.Arg(0) == "index" {
		select {}
	}
	http.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintln(w, "ok")
	})
	log.Fatal(http.ListenAndServe(*listen, nil))
}
