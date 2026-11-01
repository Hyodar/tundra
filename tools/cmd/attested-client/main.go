// attested-client is the relying party's check of an attested TLS port, as a command:
// it prints what tools/pkg/verify found and exits 0 when the verdict passes, 1 when it does
// not and 2 on a refusal (the evidence was not bound to the session, or the certificate's
// key is not one the quote binds).
package main

import (
	"context"
	"fmt"
	"os"
	"strings"

	"github.com/Hyodar/tundra/tools/pkg/verify"
	"github.com/spf13/cobra"
)

func main() {
	var opts verify.Options
	root := &cobra.Command{
		Use:   "attested-client https://HOST:PORT",
		Short: "Check that an attested TLS port's certificate belongs to the TD its quote describes",
		Long: "Connect to the attested TLS port, fetch evidence from the image's attested endpoint " +
			"(HOST:8745) on a fresh nonce, refuse evidence not bound to that session, check the " +
			"certificate's key is one the quote binds, and judge the quote against --policy. " +
			"Verdicts: trusted, partial, measurements-match, measurements-mismatch, simulated " +
			"(passes with --dev only), untrusted.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			result, err := verify.AttestedTLS(context.Background(), args[0], opts)
			if err != nil {
				return err
			}
			fmt.Fprintf(cmd.OutOrStdout(), "certificate  %s  %s  (bound to the quote for :%d)\n",
				result.Address, result.Fingerprint, result.BoundPort)
			fmt.Fprintf(cmd.OutOrStdout(), "attestation  %s, signature %s\n", result.Platform, result.Signature)
			for _, name := range verify.Registers {
				value := result.Registers[name]
				if value == "" {
					value = "-"
				}
				fmt.Fprintf(cmd.OutOrStdout(), "%-6s %-9s  %s\n", name, result.Matched[name], value)
			}
			if result.Debug {
				fmt.Fprintln(cmd.OutOrStdout(), "debug: the TD has the DEBUG attribute")
			}
			if len(result.Reasons) > 0 {
				fmt.Fprintf(cmd.OutOrStdout(), "reasons: %s\n", strings.Join(result.Reasons, ", "))
			}
			fmt.Fprintf(cmd.OutOrStdout(), "verdict: %s\n", result.Verdict)
			if !result.Passed {
				os.Exit(1)
			}
			return nil
		},
	}
	root.Flags().StringVar(&opts.Policy, "policy", "", "a tundravm.policy/2 file: the registers the quote may hold")
	root.Flags().StringVar(&opts.Endpoint, "endpoint", "", "the attested endpoint's HOST:PORT (default: the URL's host on 8745)")
	root.Flags().BoolVar(&opts.Development, "dev", false, "a development build: simulated evidence passes")
	root.Flags().DurationVar(&opts.Timeout, "timeout", 0, "per connection (default 30s)")
	if err := root.Execute(); err != nil {
		fmt.Fprintf(os.Stderr, "refused: %v\n", strings.TrimPrefix(err.Error(), "refused: "))
		os.Exit(2)
	}
}
