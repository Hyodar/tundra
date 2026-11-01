package main

import (
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"syscall"

	"github.com/Hyodar/tundra/tools/pkg/endpoint"
	"github.com/coreos/go-systemd/v22/daemon"
	"github.com/spf13/cobra"
)

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	var config string
	root := &cobra.Command{
		Use:   "attested-endpoint",
		Short: "The guest's control plane: quote-bound TLS evidence, credentials and unlock",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			cfg, err := endpoint.LoadConfig(config)
			if err != nil {
				return err
			}
			server, err := endpoint.New(cfg, endpoint.SocketQuoter{Path: cfg.QuoteSocket}, logger)
			if err != nil {
				return err
			}
			listener, err := net.Listen("tcp", cfg.Listen)
			if err != nil {
				return fmt.Errorf("listening on %s: %w", cfg.Listen, err)
			}
			logger.Info("attested endpoint listening", "addr", listener.Addr().String())
			if _, err := daemon.SdNotify(false, daemon.SdNotifyReady); err != nil {
				logger.Warn("no readiness notification", "error", err)
			}
			errs := make(chan error, 1)
			go func() { errs <- server.Serve(listener) }()
			signals := make(chan os.Signal, 1)
			signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
			select {
			case sig := <-signals:
				logger.Info("stopping", "signal", sig.String())
				return listener.Close()
			case err := <-errs:
				return err
			}
		},
	}
	root.Flags().StringVarP(&config, "config", "c", "/etc/tdx/endpoint.yaml", "config file path")
	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}
