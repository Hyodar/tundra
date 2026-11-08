The Go tools tundravm builds into images (tdxs, key-gen, disk-setup, attested-endpoint, image-metrics): their own Go module, never part of the Python package.
Copied from github.com/Hyodar/tundra-tools at cbc33ec538aaa60e54e2e889eacebb9b0c5ab479.
`logincli/cli.go` is the runtime of a login's CLI, which tundravm copies verbatim beside the `main.go` and `dev.go` it generates per login; `logincli/main.go` and `dev.go` are the flashbox login's program, built and tested here.
