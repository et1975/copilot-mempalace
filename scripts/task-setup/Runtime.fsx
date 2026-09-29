module TaskSetup

open System
open System.IO
open System.Diagnostics
open System.Text
open System.Text.Json
open System.Text.Json.Nodes
open System.Threading
open System.Threading.Tasks

exception SetupException of code: string

[<RequireQualifiedAccess>]
module Json =
    let require condition code =
        if not condition then raise (SetupException code)

    let parse (text: string) =
        try JsonNode.Parse(text).AsObject()
        with
        | :? JsonException
        | :? InvalidOperationException
        | :? NullReferenceException -> raise (SetupException "malformed_response")

    let text (name: string) (value: JsonObject) =
        match value[name] with
        | null -> ""
        | node -> node.GetValue<string>()

    let boolean (name: string) (value: JsonObject) =
        match value[name] with
        | null -> false
        | node -> node.GetValue<bool>()

    let objectOf value = JsonSerializer.Serialize(value) |> parse

    let identity name value =
        let identity = text name value
        match Guid.TryParse(identity) with
        | true, parsed when parsed <> Guid.Empty && parsed.ToString() = identity -> identity
        | _ -> raise (SetupException "invalid_identity")

type Invocation =
    { Mode: string
      Config: string
      TaskExecutable: string option
      CopilotExecutable: string option
      HubUrl: string option
      HubTokenFile: string option
      NewAuthority: bool
      Initialize: bool
      GenerateToken: bool
      RegisterCopilot: bool }

[<RequireQualifiedAccess>]
module Invocation =
    let private absolute (path: string) =
        Json.require
            (Path.IsPathFullyQualified(path) && Path.GetFullPath(path) = path)
            "absolute_path_required"
        path

    let private options (arguments: string array) =
        let rec loop (values: Map<string, string>) (remaining: string list) =
            match remaining with
            | [] -> values
            | name :: tail ->
                Json.require (not (Map.containsKey name values)) "duplicate_option"
                match name, tail with
                | ("--new-authority" | "--initialize" | "--generate-token"
                  | "--register-copilot"), _ -> loop (Map.add name "" values) tail
                | ("--config" | "--task-executable" | "--copilot-executable"
                  | "--hub-url" | "--hub-token-file"), value :: rest ->
                    Json.require (not (value.StartsWith("--"))) "missing_option_value"
                    loop (Map.add name value values) rest
                | _ -> raise (SetupException "invalid_invocation")
        loop Map.empty (Array.toList arguments)

    let private flags mode values =
        let has name = Map.containsKey name values
        let configureFlags = [ "--hub-url"; "--hub-token-file"; "--new-authority" ]
        let enableFlags = [ "--initialize"; "--generate-token"; "--register-copilot" ]
        Json.require
            (mode = "configure" || not (List.exists has configureFlags)) "invalid_invocation"
        Json.require (mode = "enable" || not (List.exists has enableFlags)) "invalid_invocation"
        Json.require (has "--hub-url" = has "--new-authority") "new_authority_pair_required"
        Json.require
            (not (has "--generate-token") || has "--initialize") "initialize_required"

    let parse (arguments: string array) =
        Json.require (arguments.Length > 0) "usage"
        let mode = arguments[0]
        Json.require (List.contains mode [ "check"; "configure"; "enable" ]) "usage"
        let values = options arguments[1..]
        flags mode values
        let has name = Map.containsKey name values
        let optional name = Map.tryFind name values
        let path name = optional name |> Option.map absolute
        let config = path "--config" |> Option.defaultWith (fun () ->
            raise (SetupException "config_required"))
        { Mode = mode; Config = config; TaskExecutable = path "--task-executable"
          CopilotExecutable = path "--copilot-executable"; HubUrl = optional "--hub-url"
          HubTokenFile = path "--hub-token-file"; NewAuthority = has "--new-authority"
          Initialize = has "--initialize"; GenerateToken = has "--generate-token"
          RegisterCopilot = has "--register-copilot" }

[<RequireQualifiedAccess>]
module PrivatePath =
    let private directoryMode =
        UnixFileMode.UserRead ||| UnixFileMode.UserWrite ||| UnixFileMode.UserExecute

    let private fileMode = UnixFileMode.UserRead ||| UnixFileMode.UserWrite

    let rec inspect (path: string) =
        Json.require (Path.IsPathFullyQualified(path)) "absolute_path_required"
        let info = FileInfo(path)
        Json.require (isNull info.LinkTarget) "symlink_refused"
        if File.Exists(path) || Directory.Exists(path) then
            let attributes = File.GetAttributes(path)
            Json.require
                (not (attributes.HasFlag(FileAttributes.ReparsePoint))) "symlink_refused"
            if Directory.Exists(path) then
                let mode = File.GetUnixFileMode(path)
                let writable =
                    mode.HasFlag(UnixFileMode.GroupWrite)
                    || mode.HasFlag(UnixFileMode.OtherWrite)
                Json.require
                    (not writable || (path = "/tmp" && mode.HasFlag(UnixFileMode.StickyBit)))
                    "unsafe_parent"
        let ancestor = Path.GetDirectoryName(path.TrimEnd('/'))
        if not (String.IsNullOrEmpty ancestor) && ancestor <> path then inspect ancestor

    let parent (path: string) =
        Json.require (not (String.IsNullOrEmpty(Path.GetFileName(path)))) "regular_file_required"
        inspect path
        Json.require (not (Directory.Exists(path))) "regular_file_required"
        let directory = Path.GetDirectoryName(path)
        let broad =
            [ "/"; "/tmp"; "/home"
              Environment.GetFolderPath(Environment.SpecialFolder.UserProfile) ]
        Json.require (not (List.contains directory broad)) "broad_root_refused"
        if Directory.Exists(directory) then
            Json.require (File.GetUnixFileMode(directory) = directoryMode) "private_parent_required"

    let rec createParents (path: string) =
        inspect path
        if not (Directory.Exists(path)) then
            createParents (Path.GetDirectoryName(path))
            Directory.CreateDirectory(path, directoryMode) |> ignore

    let write (text: string) (path: string) =
        parent path
        let options = FileStreamOptions()
        options.Mode <- FileMode.CreateNew
        options.Access <- FileAccess.Write
        options.UnixCreateMode <- Nullable fileMode
        use stream = new FileStream(path, options)
        let bytes = Encoding.UTF8.GetBytes(text)
        stream.Write(bytes)
        stream.Flush(true)

    let temporary () =
        let root = Path.GetFullPath(Path.GetTempPath()).TrimEnd('/')
        inspect root
        let repository = Path.GetFullPath(Path.Combine(__SOURCE_DIRECTORY__, "../.."))
        Json.require
            (root <> repository && not (root.StartsWith(repository + "/")))
            "temporary_root_inside_repository"
        Directory.CreateTempSubdirectory("mempalace-task-setup-").FullName

type CommandOutput =
    { ExitCode: int
      Stdout: string
      Stderr: string }

/// One owned child, with bounded pipes/deadline and process-tree cleanup.
type Child(executable: string, arguments: string list) =
    let deadline = new CancellationTokenSource(TimeSpan.FromSeconds(20.0))
    let child = new Process()
    let mutable received = 0
    let mutable requestId = 0

    let boundedText (reader: StreamReader) = task {
        let buffer = Array.zeroCreate<char> 4096
        let text = StringBuilder()
        let mutable ended = false
        while not ended do
            let! count = reader.ReadAsync(buffer.AsMemory(), deadline.Token)
            ended <- count = 0
            Json.require (text.Length + count <= 1048576) "output_limit"
            text.Append(buffer, 0, count) |> ignore
        return text.ToString()
    }

    do
        let settings = ProcessStartInfo(executable)
        settings.UseShellExecute <- false
        settings.RedirectStandardInput <- true
        settings.RedirectStandardOutput <- true
        settings.RedirectStandardError <- true
        settings.StandardInputEncoding <- UTF8Encoding(false)
        settings.StandardOutputEncoding <- UTF8Encoding(false, true)
        settings.StandardErrorEncoding <- UTF8Encoding(false, true)
        for argument in arguments do settings.ArgumentList.Add(argument)
        child.StartInfo <- settings
        Json.require (child.Start()) "child_start_failed"
        child.StandardInput.AutoFlush <- true

    let stderr = boundedText child.StandardError

    let line () = task {
        let buffer = Array.zeroCreate<char> 1
        let text = StringBuilder()
        let mutable ended = false
        while not ended do
            if stderr.IsFaulted then
                let! _ = stderr
                ()
            let! count = child.StandardOutput.ReadAsync(buffer.AsMemory(), deadline.Token)
            Json.require (count <> 0) "unexpected_eof"
            received <- received + count
            Json.require (received <= 1048576 && text.Length < 65536) "output_limit"
            ended <- buffer[0] = '\n'
            if not ended then text.Append(buffer[0]) |> ignore
        return text.ToString()
    }

    member _.Notify(message: string) : Task = task {
        do! child.StandardInput.WriteLineAsync(message.AsMemory(), deadline.Token)
    }

    member this.Call(parameters: JsonObject) = task {
        requestId <- requestId + 1
        parameters["id"] <- JsonValue.Create(requestId)
        parameters["jsonrpc"] <- JsonValue.Create("2.0")
        do! this.Notify(parameters.ToJsonString())
        let mutable response = None
        let mutable count = 0
        while response.IsNone && count < 100 do
            let! text = line ()
            let message = Json.parse text
            Json.require (Json.text "jsonrpc" message = "2.0") "invalid_jsonrpc"
            Json.require (isNull message["error"]) "mcp_error"
            count <- count + 1
            if not (isNull message["id"]) && message["id"].ToJsonString() = string requestId then
                let result = message["result"].AsObject()
                Json.require (not (Json.boolean "isError" result)) "tool_error"
                response <- Some result
        return response |> Option.defaultWith (fun () -> raise (SetupException "message_limit"))
    }

    member _.Complete() = task {
        child.StandardInput.Close()
        let! stdout = boundedText child.StandardOutput
        do! child.WaitForExitAsync(deadline.Token)
        let! errors = stderr
        return { ExitCode = child.ExitCode; Stdout = stdout; Stderr = errors }
    }

    member _.Close() : Task = task {
        child.StandardInput.Close()
        use cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(3.0))
        do! child.WaitForExitAsync(cleanup.Token)
        let! _ = stderr
        let! trailing = boundedText child.StandardOutput
        Json.require (String.IsNullOrWhiteSpace(trailing)) "unexpected_stdout"
        Json.require (child.ExitCode = 0) "frontend_shutdown_failed"
    }

    interface IDisposable with
        member _.Dispose() =
            try
                if not child.HasExited then child.Kill(entireProcessTree = true)
                child.WaitForExit(3000) |> ignore
                deadline.Cancel()
                // Observe the bounded drain even on protocol/timeout exceptions.
                try stderr.GetAwaiter().GetResult() |> ignore
                with
                | SetupException _
                | :? OperationCanceledException
                | :? DecoderFallbackException
                | :? IOException -> ()
            finally
                child.Dispose()
                deadline.Dispose()

[<RequireQualifiedAccess>]
module Command =
    let run arguments executable = task {
        use child = new Child(executable, arguments)
        return! child.Complete()
    }

    let executable fallback supplied =
        let candidates =
            match supplied with
            | Some path -> [ path ]
            | None ->
                (Environment.GetEnvironmentVariable("PATH") |> Option.ofObj
                 |> Option.defaultValue "").Split(Path.PathSeparator)
                |> Array.filter Path.IsPathFullyQualified
                |> Array.map (fun path -> Path.Combine(path, fallback))
                |> Array.toList
        candidates
        |> List.tryFind (fun path ->
            File.Exists(path)
            && (File.GetUnixFileMode(path)
                &&& (UnixFileMode.UserExecute ||| UnixFileMode.GroupExecute
                     ||| UnixFileMode.OtherExecute)) <> enum 0)
        |> Option.defaultWith (fun () -> raise (SetupException "missing_executable"))

[<RequireQualifiedAccess>]
module Protocol =
    let private request methodName parameters =
        Json.objectOf {| method = methodName; ``params`` = parameters |}

    let private initialize (child: Child) = task {
        let parameters =
            {| protocolVersion = "2025-03-26"; capabilities = {| |}
               clientInfo = {| name = "mempalace-task-setup"; version = "1" |} |}
        let! result = child.Call(request "initialize" parameters)
        Json.require
            (List.contains (Json.text "protocolVersion" result)
                [ "2024-11-05"; "2025-03-26"; "2025-06-18"; "2025-11-25" ])
            "unsupported_protocol"
        Json.require
            (not (isNull (result["capabilities"]["tools"]))) "missing_tools_capability"
        do! child.Notify("""{"jsonrpc":"2.0","method":"notifications/initialized"}""")
    }

    let private descriptor (names: Collections.Generic.HashSet<string>) (node: JsonNode) =
        let tool = node.AsObject()
        let name = Json.text "name" tool
        Json.require
            (name.StartsWith("mptask_") && names.Add(name)
             && (name |> Seq.forall (fun letter ->
                 Char.IsAsciiLetterOrDigit(letter) || letter = '_')))
            "invalid_tool_catalog"
        Json.require
            (not (String.IsNullOrWhiteSpace(Json.text "description" tool))
             && Json.text "type" (tool["inputSchema"].AsObject()) = "object")
            "invalid_tool_descriptor"
        if not (isNull tool["outputSchema"]) then
            Json.require
                (Json.text "type" (tool["outputSchema"].AsObject()) = "object")
                "invalid_tool_descriptor"

    let private tools (child: Child) = task {
        let names = Collections.Generic.HashSet<string>()
        let cursors = Collections.Generic.HashSet<string>()
        let mutable parameters = JsonObject()
        let mutable more = true
        let mutable pages = 0
        while more && pages < 10 do
            let! result = child.Call(request "tools/list" parameters)
            pages <- pages + 1
            result["tools"].AsArray() |> Seq.iter (descriptor names)
            let cursor = Json.text "nextCursor" result
            more <- cursor <> ""
            if more then
                Json.require (cursors.Add(cursor)) "repeated_cursor"
                parameters <- Json.objectOf {| cursor = cursor |}
        Json.require (not more) "page_limit"
        for name in [ "create"; "expand"; "goal_close"; "snapshot"; "health" ] do
            Json.require (names.Contains("mptask_" + name)) "missing_required_tool"
        return names.Count
    }

    let private health (result: JsonObject) =
        if not (isNull result["structuredContent"]) then result["structuredContent"].AsObject()
        else
            result["content"].AsArray()
            |> Seq.map (fun node -> node.AsObject())
            |> Seq.filter (fun item -> Json.text "type" item = "text")
            |> Seq.map (Json.text "text")
            |> String.concat "\n"
            |> Json.parse

    let verify authorityId (child: Child) = task {
        do! initialize child
        let! count = tools child
        let parameters = {| name = "mptask_health"; arguments = {| |} |}
        let! response = child.Call(request "tools/call" parameters)
        let current = health response
        Json.require (Json.text "authority_id" current = authorityId) "authority_mismatch"
        match Guid.TryParse(Json.text "epoch_id" current) with
        | true, epoch -> Json.require (epoch <> Guid.Empty) "invalid_epoch"
        | false, _ -> raise (SetupException "invalid_epoch")
        Json.require (Json.boolean "fresh" current) "health_not_fresh"
        Json.require (Json.boolean "ok" (current["maintenance"].AsObject())) "maintenance_unhealthy"
        return count
    }
