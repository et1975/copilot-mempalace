#!/usr/bin/env -S dotnet fsi
#load "task-setup/Runtime.fsx"

open System
open System.IO
open System.Net
open System.Text.Json.Nodes
open System.Threading.Tasks

open TaskSetup

[<RequireQualifiedAccess>]
module Configuration =
    let read path =
        PrivatePath.parent path
        Json.require (File.Exists(path)) "missing_config"
        Json.require (FileInfo(path).Length > 0L) "malformed_config"
        Json.require (FileInfo(path).Length <= 1048576L) "config_too_large"
        let mode = UnixFileMode.UserRead ||| UnixFileMode.UserWrite
        Json.require (File.GetUnixFileMode(path) = mode) "private_config_required"
        let document =
            try File.ReadAllText(path) |> Json.parse
            with SetupException _ -> raise (SetupException "malformed_config")
        Json.require
            (not (isNull document["schema_version"])
             && document["schema_version"].ToJsonString() = "2") "unsupported_schema"
        document

    let validate (allowMissing: bool) (executable: string) (path: string) = task {
        let! help = executable |> Command.run [ "--help" ]
        Json.require
            (help.ExitCode = 0 && help.Stdout.Contains("validate-config")) "validator_unavailable"
        let arguments =
            [ "validate-config"; "--config"; path;
              if allowMissing then "--allow-missing-service-token" ]
        let! result = executable |> Command.run arguments
        if result.ExitCode <> 0 then
            let code =
                if result.Stderr.StartsWith("not_found:") then "missing_credential"
                else "invalid_config"
            raise (SetupException code)
        let validated = Json.parse result.Stdout
        Json.require (Json.boolean "ok" validated) "invalid_config"
        Json.require
            (validated["schema_version"].ToJsonString() = "2") "unsupported_schema"
        Json.identity "authority_id" validated |> ignore
        PrivatePath.parent (Path.Combine(Json.text "runtime_dir" validated, ".setup-readonly"))
        return validated
    }

    let private hub (value: string) =
        match Uri.TryCreate(value, UriKind.Absolute) with
        | true, uri ->
            let loopback =
                match IPAddress.TryParse(uri.Host.Trim('[', ']')) with
                | true, address -> IPAddress.IsLoopback(address)
                | false, _ -> false
            Json.require
                (loopback && List.contains uri.Scheme [ "http"; "https" ]
                 && uri.UserInfo = "" && uri.Query = "" && uri.Fragment = ""
                 && uri.Port > 0 && not (value.Contains("\\"))
                 && not (value |> Seq.exists Char.IsWhiteSpace)) "loopback_hub_required"
        | false, _ -> raise (SetupException "loopback_hub_required")

    let private template invocation =
        let identity = Guid.NewGuid().ToString()
        let directory = Path.GetDirectoryName(invocation.Config)
        let url = invocation.HubUrl |> Option.defaultWith (fun () ->
            raise (SetupException "new_authority_pair_required"))
        hub url
        let document =
            Json.objectOf
                {| schema_version = 2; authority_id = identity; hub_url = url
                   service_token_file = Path.Combine(directory, "task-token-" + identity)
                   runtime_dir = Path.Combine(directory, "task-runtime-" + identity)
                   lifecycle = "launcher"; host = "127.0.0.1"; port = 0
                   maintenance_actor = "system"; recovery_actor = "operator"
                   genesis =
                    {| actor = "operator"
                       actors =
                        {| operator = "operator"; coordinator = "coordinator"; worker = "worker"
                           supervisor = "supervisor"; system = "system" |}
                       execution_profiles = {| local = {| execution_class = "isolated" |} |}
                       supervisors =
                        {| supervisor = {| profiles = [| "local" |]; workers = [| "worker" |] |} |}
                    |} |}
        invocation.HubTokenFile
        |> Option.iter (fun path -> document["hub_token_file"] <- JsonValue.Create(path))
        document

    let private compatible invocation document =
        invocation.HubUrl |> Option.iter (fun url ->
            Json.require (Json.text "hub_url" document = url) "config_input_mismatch")
        invocation.HubTokenFile |> Option.iter (fun path ->
            Json.require (Json.text "hub_token_file" document = path) "config_input_mismatch")

    let private publish executable invocation = task {
        Json.require invocation.NewAuthority "new_authority_pair_required"
        let document = template invocation
        let temporary = PrivatePath.temporary ()
        try
            let candidate = Path.Combine(temporary, "config.json")
            PrivatePath.write (document.ToJsonString()) candidate
            let! validated = validate true executable candidate
            PrivatePath.createParents (Path.GetDirectoryName(invocation.Config))
            PrivatePath.createParents (Json.text "runtime_dir" document)
            PrivatePath.write (document.ToJsonString()) invocation.Config
            return validated
        finally
            Directory.Delete(temporary, true)
    }

    let configure executable invocation = task {
        PrivatePath.parent invocation.Config
        if not (File.Exists(invocation.Config)) then return! publish executable invocation
        else
            let document = read invocation.Config
            compatible invocation document
            return! validate true executable invocation.Config
    }

[<RequireQualifiedAccess>]
module Registration =
    let private matchesArguments invocation (registration: JsonObject) =
        let supported =
            [ [| "mcp"; "--config"; invocation.Config |]
              [| "mcp"; "--config"; invocation.Config; "--timeout"; "10s" |]
              [| "mcp"; "--timeout"; "10s"; "--config"; invocation.Config |] ]
        supported |> List.exists (fun arguments ->
            let expected = Json.objectOf {| args = arguments |}
            JsonNode.DeepEquals(registration["args"], expected["args"]))

    let private compatible taskExecutable invocation (registration: JsonObject) =
        let wildcard = Json.objectOf {| tools = [| "*" |] |}
        let empty (name: string) =
            isNull registration[name] || registration[name].ToJsonString() = "{}"
        Json.require
            (empty "env" && empty "headers") "custom_registration"
        Json.require
            (Json.text "type" registration = "local"
             && Json.text "command" registration = taskExecutable
             && matchesArguments invocation registration)
            "registration_mismatch"
        Json.require
            ((isNull registration["enabled"] || Json.boolean "enabled" registration)
             && JsonNode.DeepEquals(registration["tools"], wildcard["tools"]))
            "restricted_registration"
        let supported =
            set [ "type"; "command"; "args"; "tools"; "enabled"; "env"; "headers"
                  "source"; "timeout"; "description" ]
        Json.require
            (registration |> Seq.forall (fun field -> supported.Contains(field.Key)))
            "custom_registration"

    let inspect taskExecutable copilotExecutable invocation = task {
        let! result =
            copilotExecutable |> Command.run [ "mcp"; "get"; "mempalace-tasks"; "--json" ]
        if result.ExitCode <> 0 then
            Json.require
                (result.ExitCode = 1
                 && result.Stderr.StartsWith("Error: Server \"mempalace-tasks\" not found."))
                "registration_query_failed"
            return false
        else
            let document = Json.parse result.Stdout
            Json.require (not (isNull document["mempalace-tasks"])) "registration_query_failed"
            compatible taskExecutable invocation (document["mempalace-tasks"].AsObject())
            return true
    }

    let add taskExecutable copilotExecutable invocation : Task = task {
        let! result =
            copilotExecutable
            |> Command.run
                [ "mcp"; "add"; "mempalace-tasks"; "--"
                  taskExecutable; "mcp"; "--config"; invocation.Config ]
        Json.require (result.ExitCode = 0) "registration_add_failed"
    }

type internal Preparation =
    { Invocation: Invocation
      TaskExecutable: string
      CopilotExecutable: string
      Config: JsonObject
      AuthorityId: string
      Registered: bool }

type internal Progress() =
    let completed = ResizeArray<string>()
    member val Phase = "preflight" with get, set
    member _.Completed = completed.ToArray()
    member _.Finish(phase) = completed.Add(phase)

[<RequireQualifiedAccess>]
module Setup =
    let private capabilities executable = task {
        // Native argparse help exits without constructing a frontend or owner.
        let! help = executable |> Command.run [ "mcp"; "--help" ]
        Json.require
            (help.ExitCode = 0 && help.Stdout.Contains("--no-autostart"))
            "no_autostart_unavailable"
    }

    let private connect executable (invocation: Invocation) = task {
        return! executable
                |> Command.run [ "connect"; "--config"; invocation.Config; "--timeout"; "10s" ]
    }

    let private connection authorityId (result: CommandOutput) =
        if result.ExitCode = 0 then
            let connected = Json.parse result.Stdout
            Json.require (Json.boolean "ready" connected) "owner_unhealthy"
            Json.require
                (Json.text "authority_id" connected = authorityId) "authority_mismatch"
            true
        elif result.Stderr.StartsWith("registry_missing:") then false
        elif result.Stderr.StartsWith("binding_mismatch:") then
            raise (SetupException "binding_mismatch")
        else
            raise (SetupException "owner_unreachable")

    let private probe executable (invocation: Invocation) authorityId = task {
        if invocation.Mode = "check" then do! capabilities executable
        let arguments =
            [ "mcp"; "--config"; invocation.Config; "--timeout"; "10s"; "--no-autostart" ]
        use child = new Child(executable, arguments)
        let! count = child |> Protocol.verify authorityId
        do! child.Close()
        return count
    }

    let private initialize executable (invocation: Invocation) = task {
        let arguments =
            [ "init"; "--config"; invocation.Config;
              if invocation.GenerateToken then "--generate-token" ]
        let! result = executable |> Command.run arguments
        Json.require (result.ExitCode = 0) "initialization_failed"
        Json.require (Json.parse result.Stdout |> Json.boolean "ok") "initialization_failed"
    }

    let private start executable (invocation: Invocation) authorityId = task {
        let! result =
            executable |> Command.run [ "start"; "--config"; invocation.Config; "--timeout"; "10s" ]
        Json.require (result.ExitCode = 0) "owner_start_failed"
        Json.require (connection authorityId result) "owner_not_running"
    }

    let private remedy code =
        match code with
        | "usage" ->
            "Use configure|enable|check --config ABS_CONFIG; see sidecar/setup.md."
        | "unsupported_schema" ->
            "Schema 1 is not migrated. Preserve history/backups; select a new independent config."
        | "malformed_config" | "invalid_config" ->
            "Repair the selected config with the installed native validator; never reset history."
        | "validator_unavailable" ->
            "Install a prepared task CLI with validate-config support; setup never downloads."
        | "no_autostart_unavailable" ->
            "Install a prepared task CLI with mcp --no-autostart support; setup never downloads."
        | "registration_mismatch" | "restricted_registration" | "custom_registration" ->
            "Review/remove only mempalace-tasks explicitly, then rerun with --register-copilot."
        | "missing_credential" ->
            "Check credential paths; --initialize --generate-token creates missing service tokens."
        | "owner_not_running" ->
            "Use enable deliberately; only --initialize authorizes creating/verifying genesis."
        | "binding_mismatch" ->
            "Resolve the selected binding with native tooling; do not reset registry/history."
        | "initialization_failed" ->
            "Inspect native authority/hub diagnostics. Accepted genesis is never undone by setup."
        | "registration_add_failed" ->
            "Owner/genesis may be ready. Repair registration and rerun; no rollback occurred."
        | _ ->
            "Resolve the reported phase and rerun. No durable history or unrelated owner is undone."

    let private preflight executable (invocation: Invocation) = task {
        Configuration.read invocation.Config |> ignore
        let allowMissing = invocation.Initialize && invocation.GenerateToken
        let! config = Configuration.validate allowMissing executable invocation.Config
        if invocation.Mode = "enable" then do! capabilities executable
        let copilot = Command.executable "copilot" invocation.CopilotExecutable
        let! registered = Registration.inspect executable copilot invocation
        if invocation.Mode = "check" then Json.require registered "missing_registration"
        return
            { Invocation = invocation; TaskExecutable = executable; CopilotExecutable = copilot
              Config = config; AuthorityId = Json.identity "authority_id" config
              Registered = registered }
    }

    let private owner (progress: Progress) prepared = task {
        let invocation = prepared.Invocation
        progress.Phase <- "connect"
        let! observation = connect prepared.TaskExecutable invocation
        let live =
            if not (Json.boolean "service_token_present" prepared.Config)
               && invocation.Initialize && invocation.GenerateToken
               && observation.ExitCode <> 0
               && observation.Stderr.StartsWith("not_found:") then false
            else connection prepared.AuthorityId observation
        if invocation.Mode = "check" then Json.require live "owner_not_running"
        elif not live then
            if invocation.Initialize then
                progress.Phase <- "initialize"
                do! initialize prepared.TaskExecutable invocation
                progress.Finish("initialize")
            progress.Phase <- "start"
            do! start prepared.TaskExecutable invocation prepared.AuthorityId
            progress.Finish("start")
        progress.Finish("connected")
    }

    let private registration (progress: Progress) prepared = task {
        progress.Phase <- "registration"
        let invocation = prepared.Invocation
        if invocation.Mode = "enable" && invocation.RegisterCopilot && not prepared.Registered then
            // Recheck before add; never knowingly replace a concurrent edit.
            let! exists =
                Registration.inspect prepared.TaskExecutable prepared.CopilotExecutable invocation
            if not exists then
                do! Registration.add prepared.TaskExecutable prepared.CopilotExecutable invocation
            let! exists =
                Registration.inspect prepared.TaskExecutable prepared.CopilotExecutable invocation
            Json.require exists "registration_add_failed"
            progress.Finish("registration")
            return true
        else return prepared.Registered
    }

    let private readiness (progress: Progress) prepared = task {
        progress.Finish("preflight")
        do! owner progress prepared
        let! registered = registration progress prepared
        progress.Phase <- "stdio"
        let! count = probe prepared.TaskExecutable prepared.Invocation prepared.AuthorityId
        progress.Finish("stdio")
        return
            Json.objectOf
                {| ok = true; mode = prepared.Invocation.Mode; phase = "ready"
                   authority_id = prepared.AuthorityId; completed = progress.Completed
                   service_ready = true; tool_count = count; registration_verified = registered
                   current_session = "reload_required_if_tools_not_visible"
                   native_supervision = "unavailable_not_assessed" |}
    }

    let private summary (progress: Progress) (invocation: Invocation) = task {
        Json.require (OperatingSystem.IsLinux()) "linux_required"
        let executable = Command.executable "mempalace-tasks" invocation.TaskExecutable
        if invocation.Mode = "configure" then
            progress.Phase <- "configure"
            let! config = Configuration.configure executable invocation
            return
                Json.objectOf
                    {| ok = true; mode = invocation.Mode; phase = progress.Phase
                       authority_id = Json.identity "authority_id" config
                       service_ready = false; registration_verified = false
                       native_supervision = "unavailable_not_assessed" |}
        else
            let! prepared = preflight executable invocation
            return! readiness progress prepared
    }

    let private failure (error: exn) =
        match error with
        | SetupException code -> code
        | :? OperationCanceledException -> "deadline_exceeded"
        | :? UnauthorizedAccessException -> "permission_denied"
        | :? IOException -> "filesystem_or_pipe_error"
        | :? ComponentModel.Win32Exception -> "executable_failed"
        | _ -> "invalid_response"

    let run (invocation: Invocation) = task {
        let progress = Progress()
        try
            let! result = summary progress invocation
            Console.WriteLine(result.ToJsonString())
            return 0
        with error ->
            let code = failure error
            let result =
                {| ok = false; mode = invocation.Mode; phase = progress.Phase; code = code
                   completed = progress.Completed; repair = remedy code |}
            Console.Error.WriteLine(code + ": " + remedy code)
            Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(result))
            return 1
    }

try
    let invocation = Invocation.parse fsi.CommandLineArgs[1..]
    exit ((Setup.run invocation).GetAwaiter().GetResult())
with
| SetupException code ->
    Console.Error.WriteLine(code + ": See sidecar/setup.md for supported command forms.")
    Console.WriteLine(
        System.Text.Json.JsonSerializer.Serialize(
            {| ok = false; phase = "arguments"; code = code |}))
    exit 2
