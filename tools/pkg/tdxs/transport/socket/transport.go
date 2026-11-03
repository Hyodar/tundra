package socket

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/user"
	"strconv"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/logger"
	"github.com/Hyodar/tundra/tools/pkg/tdxs/transport"
	"github.com/coreos/go-systemd/v22/activation"
	"github.com/coreos/go-systemd/v22/daemon"
)

type SocketTransport struct {
	transport.Transport

	cfg      *SocketTransportConfig
	queues   *transport.TransportQueues
	listener net.Listener
	logger   logger.Logger
	// peer reads a connection's uid and lookup names it: SO_PEERCRED and /etc/passwd.
	peer   func(net.Conn) (uint32, error)
	lookup func(string) (*user.User, error)
}

type SocketTransportConfig struct {
	Systemd  bool        `yaml:"systemd"`
	FilePath string      `yaml:"file_path"`
	Owner    string      `yaml:"owner"`
	Group    string      `yaml:"group"`
	Perm     os.FileMode `yaml:"perm"`
}

func (c *SocketTransportConfig) Validate() error {
	if c.Systemd {
		if c.FilePath != "" || c.Owner != "" || c.Group != "" || c.Perm != 0 {
			return fmt.Errorf("when systemd is true, file_path, owner, group, and perm must not be set")
		}
	} else {
		if c.FilePath == "" {
			return fmt.Errorf("file_path is required when systemd is false")
		}
	}

	return nil
}

func NewSocketTransport(cfg *SocketTransportConfig, logger logger.Logger) (transport.Transport, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}

	return &SocketTransport{
		cfg:    cfg,
		logger: logger,
		peer:   peerUID,
		lookup: user.LookupId,
	}, nil
}

func (t *SocketTransport) Start(ctx context.Context, queues *transport.TransportQueues) error {
	t.queues = queues

	var listener net.Listener
	var err error

	if t.cfg.Systemd {
		listeners, err := activation.Listeners()
		if err != nil {
			return fmt.Errorf("failed to get systemd listeners: %w", err)
		}
		if len(listeners) != 1 {
			return fmt.Errorf("expected exactly 1 systemd listener, got %d", len(listeners))
		}
		listener = listeners[0]
		t.logger.Info("Using systemd socket activation")
	} else {
		// Only remove the path if it exists and is a socket, to avoid
		// accidentally wiping directories or regular files.
		if info, err := os.Lstat(t.cfg.FilePath); err == nil {
			if info.Mode()&os.ModeSocket != 0 {
				_ = os.Remove(t.cfg.FilePath)
			} else {
				return fmt.Errorf("socket path %s exists and is not a socket", t.cfg.FilePath)
			}
		}

		listener, err = net.Listen("unix", t.cfg.FilePath)
		if err != nil {
			return fmt.Errorf("failed to create socket: %w", err)
		}

		if t.cfg.Perm != 0 {
			if err := os.Chmod(t.cfg.FilePath, t.cfg.Perm); err != nil {
				listener.Close()
				return fmt.Errorf("failed to set socket permissions: %w", err)
			}
		}

		if t.cfg.Owner != "" || t.cfg.Group != "" {
			if err := t.setOwnership(); err != nil {
				listener.Close()
				return fmt.Errorf("failed to set socket ownership: %w", err)
			}
		}

		t.logger.Info("Socket created", "path", t.cfg.FilePath)
	}

	t.listener = listener
	go t.acceptConnections(ctx)

	if t.cfg.Systemd {
		sent, err := daemon.SdNotify(false, daemon.SdNotifyReady)
		if err != nil {
			t.logger.Warn("Failed to notify systemd", "error", err)
		} else if sent {
			t.logger.Info("Notified systemd that service is ready")
		}
	}

	return nil
}

func (t *SocketTransport) acceptConnections(ctx context.Context) {
	for {
		conn, err := t.listener.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return
			default:
				// Log error and continue
				continue
			}
		}

		go t.handleConnection(ctx, conn)
	}
}

func (t *SocketTransport) handleConnection(ctx context.Context, conn net.Conn) {
	defer conn.Close()

	decoder := json.NewDecoder(conn)
	encoder := json.NewEncoder(conn)
	// Who asks comes from the kernel, never from the request: the issuer binds it.
	caller, callerErr := t.identify(conn)

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		var req SocketTransportRequest
		if err := decoder.Decode(&req); err != nil {
			if err == io.EOF {
				return
			}
			// A decoder that failed keeps failing: answer once and hang up, or the loop spins.
			encoder.Encode(NewIssueResponseFromError(fmt.Errorf("failed to decode request: %w", err)))
			return
		}

		apiRequest, err := req.UnmarshalData()
		if err != nil {
			encoder.Encode(NewIssueResponseFromError(fmt.Errorf("failed to unmarshal request data: %w", err)))
			continue
		}

		switch req.Method {
		case SocketTransportRequestMethodIssue:
			issueReq := apiRequest.(*api.IssueRequest)
			tagged, err := tag(caller, callerErr, issueReq.Caller, issueReq.UserData)
			if err != nil {
				t.logger.Warn("Refused a quote", "error", err)
				encoder.Encode(NewIssueResponseFromError(err))
				continue
			}
			issueReq.UserData = tagged
			wrapper := &api.IssueRequestWrapper{
				Request:  issueReq,
				Response: make(chan *api.IssueResponse, 1),
			}

			select {
			case t.queues.IssueQueue <- wrapper:
				select {
				case resp := <-wrapper.Response:
					encoder.Encode(NewIssueResponseFromAPI(resp))
				case <-ctx.Done():
					encoder.Encode(NewIssueResponseFromError(fmt.Errorf("context cancelled")))
					return
				}
			case <-ctx.Done():
				encoder.Encode(NewIssueResponseFromError(fmt.Errorf("context cancelled")))
				return
			}

		case SocketTransportRequestMethodMetadata:
			metadataReq := apiRequest.(*api.MetadataRequest)
			wrapper := &api.MetadataRequestWrapper{
				Request:  metadataReq,
				Response: make(chan *api.MetadataResponse, 1),
			}

			select {
			case t.queues.MetadataQueue <- wrapper:
				select {
				case resp := <-wrapper.Response:
					encoder.Encode(NewMetadataResponseFromAPI(resp))
				case <-ctx.Done():
					encoder.Encode(NewMetadataResponseFromError(fmt.Errorf("context cancelled")))
					return
				}
			case <-ctx.Done():
				encoder.Encode(NewMetadataResponseFromError(fmt.Errorf("context cancelled")))
				return
			}

		case SocketTransportRequestMethodValidate:
			validateReq := apiRequest.(*api.ValidateRequest)
			wrapper := &api.ValidateRequestWrapper{
				Request:  validateReq,
				Response: make(chan *api.ValidateResponse, 1),
			}

			select {
			case t.queues.ValidateQueue <- wrapper:
				select {
				case resp := <-wrapper.Response:
					encoder.Encode(NewValidateResponseFromAPI(resp))
				case <-ctx.Done():
					encoder.Encode(NewValidateResponseFromError(fmt.Errorf("context cancelled")))
					return
				}
			case <-ctx.Done():
				encoder.Encode(NewValidateResponseFromError(fmt.Errorf("context cancelled")))
				return
			}

		default:
			encoder.Encode(NewIssueResponseFromError(fmt.Errorf("unknown method: %s", req.Method)))
		}
	}
}

func (t *SocketTransport) setOwnership() error {
	uid := -1
	gid := -1

	// Resolve user ID
	if t.cfg.Owner != "" {
		u, err := user.Lookup(t.cfg.Owner)
		if err != nil {
			return fmt.Errorf("failed to lookup user %s: %w", t.cfg.Owner, err)
		}
		uid, err = strconv.Atoi(u.Uid)
		if err != nil {
			return fmt.Errorf("failed to parse UID: %w", err)
		}
	}

	// Resolve group ID
	if t.cfg.Group != "" {
		g, err := user.LookupGroup(t.cfg.Group)
		if err != nil {
			return fmt.Errorf("failed to lookup group %s: %w", t.cfg.Group, err)
		}
		gid, err = strconv.Atoi(g.Gid)
		if err != nil {
			return fmt.Errorf("failed to parse GID: %w", err)
		}
	}

	// Apply ownership
	if uid != -1 || gid != -1 {
		if err := os.Chown(t.cfg.FilePath, uid, gid); err != nil {
			return err
		}
	}

	return nil
}
