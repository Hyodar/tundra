#!/usr/bin/env bash
set -euo pipefail

# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"tdxs","package":"./cmd/tdxs","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/tdxs","kind":"file","mode":"0755","path":"build/tdxs"}],"name":"tdxs","source":{"kind":"local","path":"tundravm:tools"}}
# unpinned: tundravm:tools
echo 'tundravm: source build tdxs is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
# tundravm build: {"build":{"env":{"DOTNET_CLI_HOME":"/tmp/dotnet","DOTNET_CLI_TELEMETRY_OPTOUT":"1","DOTNET_NOLOGO":"1","DOTNET_SKIP_FIRST_TIME_EXPERIENCE":"1","NUGET_PACKAGES":"/tmp/nuget"},"kind":"script","output":"publish/nethermind","packages":["dotnet-sdk-10.0","build-essential","git"],"script":"dotnet restore src/Nethermind/Nethermind.Runner --runtime linux-x64 --disable-parallel --force && dotnet publish src/Nethermind/Nethermind.Runner --configuration Release --runtime linux-x64 --self-contained true --output publish -p:Deterministic=true -p:ContinuousIntegrationBuild=true -p:PublishSingleFile=true -p:BuildTimestamp=0 -p:Commit=0000000000000000000000000000000000000000 -p:PublishReadyToRun=false -p:DebugType=none -p:IncludeAllContentForSelfExtract=true -p:EmbedUntrackedSources=true"},"cache_key":"nethermind-1.32.3-linux-x64","install":[{"dest":"/usr/lib/nethermind/nethermind","kind":"file","mode":"0755","path":"publish/nethermind"},{"dest":"/usr/lib/nethermind/NLog.config","kind":"file","mode":"0644","path":"publish/NLog.config"},{"dest":"/usr/lib/nethermind/plugins","kind":"tree","path":"publish/plugins"}],"name":"nethermind","source":{"kind":"git","ref":"1.32.3","submodules":false,"url":"https://github.com/NethermindEth/nethermind.git"}}
# unpinned: 1.32.3
echo 'tundravm: source build nethermind is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"signer","package":".","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/signer","kind":"file","mode":"0755","path":"build/signer"}],"name":"signer","source":{"kind":"local","path":"signer"}}
# unpinned: signer
echo 'tundravm: source build signer is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"key-gen","package":"./cmd/key-gen","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/key-gen","kind":"artifact","mode":"0755"}],"name":"key-generation","source":{"kind":"local","path":"tundravm:tools"}}
# unpinned: tundravm:tools
echo 'tundravm: source build key-generation is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"disk-setup","package":"./cmd/disk-setup","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/disk-setup","kind":"artifact","mode":"0755"}],"name":"disk-encryption","source":{"kind":"local","path":"tundravm:tools"}}
# unpinned: tundravm:tools
echo 'tundravm: source build disk-encryption is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
# tundravm build: {"build":{"env":{},"kind":"go","ldflags":"-s -w -buildid=","output":"attested-endpoint","package":"./cmd/attested-endpoint","packages":["golang"],"tags":[]},"install":[{"dest":"/usr/bin/attested-endpoint","kind":"artifact","mode":"0755"}],"name":"attested-endpoint","source":{"kind":"local","path":"tundravm:tools"}}
# unpinned: tundravm:tools
echo 'tundravm: source build attested-endpoint is not pinned: run tundravm lock, then tundravm build' >&2 && exit 1
