#!/usr/bin/env -S dotnet fsi
#load "../../scripts/task-setup/Runtime.fsx"

open TaskSetup

let expectCode expected action =
    try
        action ()
        failwithf "Expected %s" expected
    with SetupException actual when actual = expected -> ()

expectCode "usage" (fun () -> Invocation.parse [||] |> ignore)
expectCode "initialize_required" (fun () ->
    Invocation.parse [| "enable"; "--config"; "/private/config"; "--generate-token" |] |> ignore)
expectCode "new_authority_pair_required" (fun () ->
    Invocation.parse [| "configure"; "--config"; "/private/config"; "--new-authority" |] |> ignore)
expectCode "duplicate_option" (fun () ->
    Invocation.parse [| "check"; "--config"; "/one"; "--config"; "/two" |] |> ignore)
expectCode "absolute_path_required" (fun () ->
    Invocation.parse [| "check"; "--config"; "relative" |] |> ignore)
for mode in [ "configure"; "enable"; "check" ] do
    let selected =
        Invocation.parse
            [| mode; "--config"; "/private/config"
               "--task-executable"; "/selected/mempalace-tasks"
               "--copilot-executable"; "/selected/copilot" |]
    assert (selected.TaskExecutable = Some "/selected/mempalace-tasks")
    assert (selected.CopilotExecutable = Some "/selected/copilot")

let request = Invocation.parse [| "check"; "--config"; "/private/config" |]
assert (not request.Initialize && not request.GenerateToken && not request.RegisterCopilot)
printfn "F# invocation contracts passed"
