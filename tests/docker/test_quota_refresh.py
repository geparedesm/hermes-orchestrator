"""Real Docker quota isolation and credential write-back with account-protocol stand-ins, never real identities."""

from __future__ import annotations

import json

import pytest

from agent_manager import quota, stack
from conftest import credential_volume  # type: ignore[import-not-found]

pytestmark = pytest.mark.docker
NODE = "/opt/ho/pen-node/bin/node"


@pytest.fixture(autouse=True)
def docker_api_timeout(client, monkeypatch):
    # Image builds/backups on a shared Docker host can delay volume/container removal. The probe's own
    # account-read deadline is unchanged; give fixture setup and cleanup time to finish.
    monkeypatch.setattr(client.api, "timeout", 180)


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("renewed", [False, True])
def test_probe_filters_output_cleans_up_and_never_overwrites_new_login(ops, client, tmp_path, monkeypatch, provider, renewed):
    filename = "auth.json" if provider == "codex" else ".credentials.json"
    original = {"claudeAiOauth": {"accessToken": "test-only", "scopes": ["user:profile"]}, "version": "old"}
    volume = credential_volume(client, provider, "hotest", {filename: json.dumps(original)})
    stub = """
const fs=require('fs');
const observed=Date.now()/1000;
const source=process.env.CODEX_HOME ? process.env.CODEX_HOME+'/auth.json' : process.env.CLAUDE_CONFIG_DIR+'/.credentials.json';
const cred=JSON.parse(fs.readFileSync(source));cred.version='refreshed';fs.writeFileSync(source,JSON.stringify(cred));
"""
    # Both variables are inherited from the image? Choose by the provider rather than relying on the unused one.
    stub = stub.replace("process.env.CODEX_HOME ?", "true ?" if provider == "codex" else "false ?")
    if renewed:
        stub += f"fs.writeFileSync('/run/ho-credentials/{provider}/{filename}',JSON.stringify({{version:'operator'}}));\n"
    if provider == "codex":
        stub += """
require('readline').createInterface({input:process.stdin}).on('line',(line)=>{
 const req=JSON.parse(line);
 if(req.id===1) console.log(JSON.stringify({id:1,result:{userAgent:'test'}}));
 if(req.id===2) console.log(JSON.stringify({id:2,result:{private:'must not escape',rateLimits:{
  primary:{windowDurationMins:300,usedPercent:15,resetsAt:observed+3600},
  secondary:{windowDurationMins:10080,usedPercent:35,resetsAt:observed+86400}}}}));
});
"""
    else:
        stub += """
console.log(JSON.stringify({type:'assistant',private:'must not escape',usage_report:{rate_limits:{limits:[
 {kind:'session',percent:15,resets_at:new Date((observed+3600)*1000).toISOString()},
 {kind:'weekly_all',percent:35,resets_at:new Date((observed+86400)*1000).toISOString()}]}}}));
console.log(JSON.stringify({type:'result',local_command:'usage',num_turns:0,duration_api_ms:0,modelUsage:{},is_error:false}));
"""
    # The probe tmpfs can be mounted noexec. Invoke the protocol stand-in through the pinned Node interpreter,
    # while keeping the actual credential, flock/CAS, container and network boundaries under test.
    prefix = "{const fs=require('fs'),cp=require('child_process'),original=cp.spawn;fs.writeFileSync('/tmp/cli.cjs'," + \
             json.dumps(stub) + ",{mode:0o600});cp.spawn=(command,args,options)=>{if(command!==" + json.dumps(provider) + \
             ")throw Error('unexpected command');return original(" + json.dumps(NODE) + ",['/tmp/cli.cjs',...args],options);};}\n"
    script = tmp_path / "probe.cjs"
    script.write_text(prefix + quota.SCRIPT.read_text())
    monkeypatch.setattr(quota, "SCRIPT", script)
    result = quota.refresh(ops, provider, "hotest")
    assert result["status"] == ("BUSY" if renewed else "OK"), result
    assert "must not escape" not in json.dumps(result)
    if not renewed:
        assert [w["used_percent"] for w in result["windows"]] == [15, 35]
    # Read only the synthetic credential we created, through a networkless helper.
    saved = client.containers.run(ops.ensure_image("agent-base").id, ["cat", "/c/" + filename],
                                  network_mode="none", volumes={volume: {"bind": "/c", "mode": "ro"}}, remove=True)
    assert json.loads(saved)["version"] == ("operator" if renewed else "refreshed")
    assert not client.containers.list(all=True, filters={"label": [stack.LABEL, "ho.quota_probe"]})
    assert not client.networks.list(filters={"label": [stack.LABEL, "ho.quota_probe"]})


def test_legacy_claude_token_reports_profile_requirement_without_a_model(ops, client):
    credential_volume(client, "claude", "hotest", {"oauth_token": "test-only"})
    assert quota.refresh(ops, "claude", "hotest") == {"status": "PROFILE_REQUIRED", "windows": []}
