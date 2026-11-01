// Command key-gen generates and manages cryptographic keys for TDX VM
// initialization.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"

	"github.com/Hyodar/tundra/tools/pkg/exec"
	"github.com/Hyodar/tundra/tools/pkg/keys"
	"github.com/Hyodar/tundra/tools/pkg/tpm"
	"github.com/spf13/cobra"
)

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

	root := &cobra.Command{
		Use:   "key-gen",
		Short: "Key generation and management for TDX VM initialization",
	}

	root.AddCommand(setupCmd())
	root.AddCommand(validateCmd())

	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}

func setupCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "setup <config.yaml>",
		Short: "Generate or retrieve all keys defined in the config",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			cfgPath := args[0]

			cfg, err := keys.LoadConfig(cfgPath)
			if err != nil {
				return fmt.Errorf("load config: %w", err)
			}

			if err := cfg.Validate(); err != nil {
				return fmt.Errorf("validate config: %w", err)
			}

			t := tpm.NewDevice(&exec.DefaultRunner{})
			mgr := keys.NewManager(cfg, t)

			ctx := context.Background()
			result, err := mgr.GenerateAll(ctx)
			if err != nil {
				return fmt.Errorf("generate keys: %w", err)
			}

			if err := keys.WriteOutputs(cfg, result); err != nil {
				return fmt.Errorf("write key outputs: %w", err)
			}

			for name, key := range result {
				slog.Info("key generated", "name", name, "length", len(key))
			}

			return nil
		},
	}
}

func validateCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "validate <config.yaml>",
		Short: "Parse and validate a key-gen configuration file",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			cfgPath := args[0]

			cfg, err := keys.LoadConfig(cfgPath)
			if err != nil {
				return fmt.Errorf("load config: %w", err)
			}

			if err := cfg.Validate(); err != nil {
				return fmt.Errorf("validate config: %w", err)
			}

			fmt.Fprintf(cmd.OutOrStdout(), "Configuration is valid.\n")
			fmt.Fprintf(cmd.OutOrStdout(), "Keys defined: %d\n", len(cfg.Keys))
			for name, kc := range cfg.Keys {
				fmt.Fprintf(cmd.OutOrStdout(), "  - %s: strategy=%s tpm=%v\n", name, kc.Strategy, kc.TPM)
			}

			return nil
		},
	}
}
