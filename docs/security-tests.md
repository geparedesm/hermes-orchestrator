# Security and failure tests

Where each scenario of MASTER_SPEC §89 and each absolute rule of §91 is proven. `make test-security` runs the security suite listed in `tests/security_suite.txt` (integration tests plus real-Docker tests); the end-to-end smoke tests (`scripts/smoke-phaseN.sh`) inject real failures on throwaway Compose stacks.

## §89 scenarios

| Scenario | Proven by |
| --- | --- |
| Worker crashes | `smoke-phase8.sh` (worker removed while the control plane was down → `LOST`); `test_recovery.py::test_startup_reconciles_executions_with_agent_manager` |
| Claude crashes / unavailable | `test_orchestration.py::test_orchestrator_failures_fail_over_then_block`, `test_orchestrator_login_failure_fails_over_without_waiting`; `smoke-phase7.sh` (failover to Codex, new epoch) |
| Codex unavailable | `smoke-phase7.sh` (both providers fail → `AUTH_REQUIRED`, no loss); `test_orchestration.py` QUOTA fallback paths |
| Redis restart | `smoke-phase8.sh`, `smoke-phase11.sh` (scheduling continues from PostgreSQL) |
| PostgreSQL reconnect | `smoke-phase11.sh` (database stopped: readiness `unavailable`; restarted: new tasks accepted, scheduler resumes) |
| Hermes offline | `smoke-phase9.sh` (work continues while Hermes is stopped); `test_notifications.py::test_outage_keeps_order_and_backs_off` |
| Machine restart | `smoke-phase8.sh` (whole stack restarted: tasks, checkpoints kept; new work runs); startup reconciliation |
| Task cancellation | `test_recovery.py::test_cancel_stops_work_and_retains_workspaces`; `smoke-phase8.sh` |
| Pause/resume | `test_orchestration.py::test_results_arriving_while_paused_are_not_applied` |
| Budget exhaustion | `test_orchestration.py::test_concurrent_launches_cannot_overshoot_the_budget`, `test_exhausted_budget_during_verification_pauses_and_resumes` |
| Authentication expiration | `test_agents.py::test_expired_login_waits_in_auth_required_and_resumes_after_login`; `smoke-phase4.sh` (real CLIs with invalid logins) |
| Git conflict | `test_git.py::test_integration_conflict_changes_nothing_and_can_be_resolved` |
| User modifies the same file | `test_git.py::test_human_changes_are_classified_and_acted_on`, `test_merge_never_overwrites_uncommitted_changes`, `test_critical_human_change_needs_an_approval` |
| Review failure | `test_orchestration.py::test_changes_requested_go_back_to_the_developer_then_an_alternate` |
| Test failure | `test_quality.py::test_failing_tests_block_ready_for_merge`; `smoke-phase6.sh` |
| Browser test failure | `smoke-phase6.sh`; `tests/docker/test_phase6.py` (Browser Runner) |
| Scope expansion | `test_orchestration.py::test_plan_expansion_needs_an_approval_bound_to_that_plan` |
| Duplicate task | `test_orchestration.py::test_duplicate_requests_wait_for_the_user`; `smoke-phase7.sh` |
| High-risk command | `test_policy.py` command classification; `test_agents.py::test_high_risk_commands_raise_an_advisory_event`; high-risk capabilities are absent from workers (AD-14) |
| Worker attempts another project's files | `test_agent_manager.py::test_forbidden_workspaces_are_rejected`, `test_symlinked_workspace_is_rejected`; `test_policy.py::test_only_orchestrator_reads_projects`; `test_dependency_cache.py::test_caches_are_never_shared_between_projects` |
| Worker attempts the Docker socket | `test_agent_manager.py::test_worker_is_hardened_and_confined` (no socket mount, read-only root, UID 10001, no capabilities) |
| Worker attempts a production secret | `test_policy.py::test_production_needs_matching_approval`; `test_phase4.py::test_file_secrets_are_in_memory_mode_0600_and_redacted` (secrets by reference, redacted) |

## §91 absolute rules

| Rule | Proven by |
| --- | --- |
| 1–3 No fork, reuse Hermes, no invented APIs | Hermes runs from its official image by digest; plugin contract verified in that image (`tests/hermes/probe.py`, `smoke-phase9.sh`) |
| 4 No Docker socket for workers | `test_worker_is_hardened_and_confined` |
| 5 No unrestricted host access | same; `test_egress_goes_only_through_the_allowlisting_proxy`, `test_runner_without_network_has_no_route` |
| 6 No shared workspaces | `test_forbidden_workspaces_are_rejected`, `test_task_service_networks_are_isolated` |
| 7 No GitHub credentials in workers | GitHub login only in Git Service (`gh-config`); `test_unknown_request_fields_cannot_add_privileges` |
| 8 No provider credentials in Git/PostgreSQL/logs | `test_credentials_and_images_are_listed_without_contents`, `test_redact.py`; backups exclude them (`smoke-phase11.sh`) |
| 9 No secrets in manifests | manifests built from records only, validated by schema; `test_secrets_are_granted_by_reference_only` |
| 10 No chain-of-thought | `test_adapters.py::test_claude_success_is_normalized_and_reasoning_dropped`, `test_codex_success_is_normalized_and_reasoning_dropped` |
| 11 No automatic merge into main | `test_git.py::test_merge_refuses_moved_target_forged_expired_or_foreign_authorizations`, `test_only_allowed_approvers_decide_merges` |
| 12 No silent overwrite of human changes | `test_merge_never_overwrites_uncommitted_changes`, `test_merge_never_overwrites_uncommitted_user_changes` |
| 13 Project config cannot bypass hard policy | `test_gitpolicy.py::test_protected_set_always_includes_main_master_and_default`, `test_config.py::test_hard_policy_clamps_protected_branches_and_findings` |
| 14 No production access by default | `test_production_needs_matching_approval` |
| 15 Claude cannot self-grant capabilities | `test_policy.py::test_developer_grant_is_intersection`; orchestrator actions are a closed vocabulary (`test_invalid_actions_are_rejected_and_fed_back`); `test_hermes_plugin.py::test_tools_are_read_and_create_only` |
| 16 Workers cannot create containers | no Docker access (rule 4); `test_unsafe_compose_is_refused_before_start` |
| 17 Redis is not the sole state | `smoke-phase8.sh`, `smoke-phase11.sh` |
| 18 Hermes not needed for authorized work | `smoke-phase9.sh` outage section |
| 19 Duplicates not silently discarded | `test_duplicate_requests_wait_for_the_user` (a duplicate waits for the user) |
| 20 No two orchestrators | `test_actions_from_a_stale_epoch_are_rejected`, `test_fenced_launch_from_an_older_epoch_is_cancelled`, `test_orphaned_tasks_are_adopted` (epochs never reused) |

## Approvals and merges

`test_only_allowed_approvers_decide_merges`, `test_human_actions_need_an_identity`, `test_plugin_must_forward_a_human_principal`, `test_approved_merge_is_verified_before_done`, `test_ready_for_merge_is_not_done`, `test_maintenance.py::test_passing_gate_requests_the_merge_approval`, `test_updates.py` (UPDATE approvals bound to the running version).
