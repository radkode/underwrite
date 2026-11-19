# Attested proof

The operator supplies a proof configuration `$G` outside every repository and session,
with exactly `version`, `profile` (the trusted profile path), `publicKey` (the pinned
P-256 key path), `adapter` (the absolute argv that runs the gateway adapter, a wrapper
when the gateway is another account), and `evidenceRoot` (the gateway's outbox). The
profile's own command is the fixed prefix, such as `/usr/local/bin/python3 -I -B -m
unittest`; you choose only what comes after it, at most 16 arguments of at most 512
bytes each, and the exit code. The adapter's configuration must opt in with
`proofArguments`. Never take any of these from the head, the PR text, or returned
evidence.

Run one before writing the beat it backs, and put `--` before the arguments:

```bash
$S/scripts/proofctl.py prove "$R" <beat> "$G" --exit-code 1 -- tests.test_app.AppTest
```

`prove` reserves the request, runs the adapter, verifies the six-file handoff the same way
`implementationctl.py consume` does (both signatures under the pinned key, the frozen
source bundle, the output tree, both complete streams, and the exact job and exit code),
keeps the request, envelopes and streams under `$R/proof-evidence/<proof>/`, and prints
the proof. It exits 0 with `status: verified`. It exits 2 with `status: failed` when the
gateway proved it will never sign this attempt, most often because the command did not
exit as predicted: the failure records the gateway's reason, and nothing was proven. A
wrong prediction is information, not evidence; write the beat from what you read, or run
a sharper command. It exits 3 with `status: retry` on an unknown outcome; repeat the same
call, which reuses the same reservation. Exit 1 is a refusal with its reason.

An exit code is coarse: a test module that fails to import exits 1 just like a failing
assertion. Before you cite a verified proof, read the signed output `prove` returns under
`output`, and cite it only when that output shows your claim, such as the assertion you
named. When it shows something else, the run proved nothing about the claim; say what it
did show, or run a sharper command.

Then write the beat with `proof_kind: "attested"` and `attested_proof: <proof>`. `PROOF`
quotes the command and what it printed, in the budget like any other slot. The store
refuses an attested beat whose proof is not verified, was run for another beat, or sits
in a session without a frozen PR target, and refuses `attested_proof` beside any other
proof kind. It then writes `attestation` itself from the verified row: the exact argv, the
exit code, the receipt digest, the key and executor, and the last lines of each stream.
Never write `attestation`; the page shows it beside the beat as what ran, so your `PROOF`
line cannot pass for the run.

Run only what a claim needs: one test module or case, a `-k` filter, a single check. A
command that needs network, an install, or a second process fails in the sandbox; that is
a limit of the evidence, not a finding about the change. The separate steps stay
available when the adapter is driven by another supervisor:

```bash
$S/scripts/proofctl.py request "$R" <beat> "$P" --exit-code 1 -- tests.test_app
$S/scripts/proofctl.py consume "$R" <proof> "$P" "$K" "$E"
$S/scripts/proofctl.py fail "$R" <proof> --reason '<the gateway reason>'
$S/scripts/proofctl.py proofs "$R"
```

`request` prints the exact gateway request; the adapter's `request.json` must be its
canonical bytes. Use `fail` only for an adapter `status: failed`, as with implementation
attempts. A proof does not change the session's `no_exec` policy, its audience, or any
delivery, and it never authorizes an effect.
