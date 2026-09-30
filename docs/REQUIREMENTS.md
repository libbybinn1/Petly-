# REQUIREMENTS — PetMatch

**Functional and non-functional requirements, each with the artifact that
proves it.**

Every requirement carries an identifier so features, tests and documentation
can cite it. `MUST` items are mandatory; `SHOULD` items are strong
recommendations from the specifications.

**Every MUST names a test, a guard or a command-line check.** A requirement
with nothing behind it is a wish, and blueprint §18 makes "no contradiction
between the documents and the code" a Definition-of-Done item. Where a
requirement is only partly met, the Proven-by column says so instead of
pointing at something that does not check it.

Sources: course blueprint §4, §12, §13, §17; PetMatch spec §4–§25.

---

## 1. Functional requirements

### FR-1 Authentication and accounts

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-1.1 | A visitor MUST be able to register an adopter account with name, email and password. | MUST | `test_valid_registration_creates_an_account` |
| FR-1.2 | Passwords MUST be stored hashed, never in plain text. | MUST | `test_the_stored_password_is_hashed` |
| FR-1.3 | A registered user MUST be able to sign in and out. | MUST | `test_valid_credentials_are_accepted`, `test_sign_out_requires_post` |
| FR-1.4 | Sign-in failure MUST NOT reveal whether the email exists. | MUST | `test_unknown_email_gives_the_same_response_as_a_wrong_password`, `test_neither_refusal_hints_that_the_account_exists` |
| FR-1.5 | A deactivated account MUST be refused sign-in, and an existing session MUST end. | MUST | `test_deactivated_account_cannot_sign_in`, `test_a_deactivated_account_loses_an_existing_session` |
| FR-1.6 | Sign-out MUST require a POST, so a third-party page cannot trigger it. | MUST | `test_sign_out_is_post_only` |
| FR-1.7 | A `next` parameter on sign-in MUST only ever redirect inside this application. | MUST | `test_an_off_site_next_target_is_refused` |

### FR-2 Roles and authorization

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-2.1 | The system MUST define exactly two roles: Adopter and Staff. | MUST | `scripts/verify_requirements.py`, check "Two roles + authorization" |
| FR-2.2 | Authorization MUST be enforced server-side on every protected route. Hiding a button is not sufficient (blueprint §12). | MUST | Every staff-only row of `docs/UX.md` §4 is resolved against `app.url_map` and its view's decorators by `scripts/verify_requirements.py` |
| FR-2.3 | An adopter MUST receive 403 on any staff route, including direct POSTs. | MUST | `test_adopter_is_refused_staff_pages`, `test_an_adopter_cannot_send_an_invitation` |
| FR-2.4 | An adopter MUST only be able to read and act on their own records. Ownership MUST be checked in the handler, not only in the URL. | MUST | `tests/api/test_qa_authorization.py::TestOneAdopterCannotActOnAnothersRecords`, `test_an_adopter_may_not_read_somebody_elses` |
| FR-2.5 | Staff MUST NOT be able to apply, withdraw, respond to an invitation or edit a profile on an adopter's behalf. | MUST | `tests/api/test_qa_authorization.py::TestEveryPostRouteRefusesTheWrongRole` |
| FR-2.6 | An anonymous visitor MUST be redirected to sign in, not shown a dead end, on every protected page. The JSON endpoint answers 401 instead. | MUST | `test_anonymous_status_matches_the_documented_table`, `test_an_anonymous_visitor_cannot_poll` |

### FR-3 Adopter profile

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-3.1 | An adopter MUST be able to record home type, yard, children, other animals, experience, activity level, daily hours available and location. | MUST | `test_complete_submission_is_accepted`, `test_valid_profile_is_saved_and_marked_complete` |
| FR-3.2 | The profile MUST include an explicit opt-in for proactive suggestions, defaulting to off (spec §5.1). | MUST | `test_opt_in_defaults_to_false`, `test_opt_in_defaults_to_off_when_the_box_is_absent` |
| FR-3.3 | The system MUST track whether a profile is complete; completeness gates Find More Adopters. | MUST | `test_incomplete_profile_refused` |
| FR-3.4 | Profile updates MUST append an `AdopterProfileUpdated` event. | MUST | `test_every_domain_event_type_is_referenced_somewhere` |
| FR-3.5 | The profile MUST record a preferred size and a preferred age band, from the closed vocabularies in `MODEL_DATA.md` §2.2. | MUST | `test_every_adopter_states_a_size_or_age_preference_and_a_known_city`, `test_any_is_treated_as_a_stated_openness` |

### FR-4 Animals

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-4.1 | Staff MUST be able to create and edit animals **through the application**. | MUST | `tests/api/test_feature_animal_lifecycle.py::TestCreating`, `TestEditing`; `test_the_animal_create_and_edit_routes_exist` |
| FR-4.2 | Every animal MUST have at least one image (spec §24), enforced on create **and** on edit. | MUST | `test_an_animal_without_a_photo_is_refused`, `test_an_edit_cannot_remove_the_last_photograph`, `test_the_guard_raises_for_an_empty_set` |
| FR-4.3 | An animal MUST record species, breed, age, size, temperament, activity level, child and animal compatibility, special needs, required space, location and status. | MUST | `test_a_complete_submission_is_accepted` |
| FR-4.4 | Staff MUST be able to change an animal's status; each change appends `AnimalStatusChanged`. | MUST | `tests/api/test_feature_animal_lifecycle.py::TestChangingStatus` |
| FR-4.5 | Animal status MUST follow the state machine in `MODEL_DATA.md` §3, and only AVAILABLE accepts applications. | MUST | `test_an_adopted_animal_leaves_the_public_search`, `test_application_to_unavailable_animal_refused` |
| FR-4.6 | Listing and updating an animal MUST append `AnimalListed` and `AnimalUpdated` respectively. | MUST | `test_every_domain_event_type_is_referenced_somewhere` |

### FR-5 Search and browsing

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-5.1 | Any visitor MUST be able to search animals by free text over name, breed and description. | MUST | `test_visitor_searches_filters_and_opens_an_animal` |
| FR-5.2 | Search MUST support filters for species, size, **age band**, activity level, city and the two compatibility flags — every filter spec §6.2 names. | MUST | `tests/api/test_feature_age_filter.py::TestTheBandsOnThePublicSearch`, `test_every_filter_combination_is_accepted`; the field count is checked by `scripts/verify_requirements.py` |
| FR-5.3 | A search result MUST link to a details view (blueprint §13). | MUST | `test_visitor_searches_filters_and_opens_an_animal` |
| FR-5.4 | Results MUST be paginated, and a malformed page value MUST NOT error. | MUST | `test_invalid_page_number_falls_back`, `test_an_astronomically_large_page_number_does_not_crash` |
| FR-5.5 | A zero-result search SHOULD explain how to widen the search rather than showing a bare message. | SHOULD | `test_empty_search_explains_how_to_widen_it` |
| FR-5.6 | An adopter MUST be able to search in natural language (spec §6.3). | MUST | `tests/api/test_feature_natural_language_search.py` |
| FR-5.7 | Natural-language search MUST be able to fuse the adopter's stored profile with the current intent, as an explicit opt-in (spec §6.4). | MUST | `test_ticking_use_profile_records_the_adopter`, `test_a_profile_bound_job_ranks_its_results` |

### FR-6 Tabular display and dashboard

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-6.1 | Staff MUST have a table view of every animal with status, species, **size** and **age** filters (blueprint 4.3, spec §7.1). | MUST | `tests/api/test_feature_age_filter.py::TestTheBandsOnTheStaffTable`, `test_staff_animal_table_lists_the_roster`. **Partly met in the interface:** the query and the route honour size and age, but `animals/manage.html` does not yet render those two selects — see FEATURES.md F-25. |
| FR-6.2 | Staff MUST have a dashboard showing available animals, pending applications, applications needing attention, open invitations, expired invitations, animals with no suitable applicants, animals with no applicants at all, and recent activity (spec §22). | MUST | `scripts/verify_requirements.py`, check "4.4 Dashboard" (all eight figures); `test_staff_dashboard_shows_operational_figures` |
| FR-6.3 | The dashboard SHOULD include a "Needs Attention" section linking figures to actions. | SHOULD | `test_a_tile_with_a_target_renders_as_a_link`, `test_a_tile_without_a_target_is_not_a_link` |
| FR-6.4 | Recent activity MUST be derived from the event log, not a separate audit table. | MUST | `test_dashboard_activity_feed_is_populated` |
| FR-6.5 | The dashboard MUST report the agent's outstanding and failed work, so a stuck queue is distinguishable from a busy one. | MUST | `test_outstanding_and_failed_work_are_reported_separately`, `test_an_empty_queue_says_so` |

### FR-7 Applications

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-7.1 | An adopter MUST be able to apply for multiple animals (spec §5.3). | MUST | `test_multiple_applications_to_different_animals_allowed` |
| FR-7.2 | An adopter MUST NOT hold two active applications for the same animal, enforced by the database and not only by a check. | MUST | `test_duplicate_application_is_refused`, `test_the_database_itself_forbids_duplicate_active_applications` |
| FR-7.3 | An application MUST only be accepted for an animal whose status is AVAILABLE — on the direct path **and** on the invitation path. | MUST | `test_cannot_apply_for_an_adopted_animal`, `test_accepting_is_refused_once_the_animal_is_no_longer_available` |
| FR-7.4 | Staff MUST be able to approve, reject, mark under review and reverse an approval, through an HTTP route. | MUST | `tests/api/test_feature_staff_decision.py::TestDecisions` |
| FR-7.5 | Approving an application MUST close **that adopter's** other active applications, recording `ApplicationClosedDueToOtherApproval` with the causing application id (spec §7.5). | MUST | `test_approval_closes_other_active_applications`, `test_closure_records_which_approval_caused_it`, `test_another_adopters_application_for_the_same_animal_is_untouched` |
| FR-7.6 | Closed applications MUST NOT be deleted; their history MUST remain queryable. | MUST | `test_nothing_is_ever_deleted`, `test_full_history_is_preserved` |
| FR-7.7 | If an approval is reversed, exactly those applications closed *because of it* MUST be eligible to reopen. Applications closed for other reasons MUST NOT be affected. | MUST | `test_a_cascade_closed_application_reopens_on_reversal`, `test_a_withdrawn_application_does_not_reopen`, `test_the_most_recent_closure_decides_what_a_reversal_reopens` |
| FR-7.8 | One animal MUST NOT be approved for two adopters. | MUST | `test_one_animal_cannot_be_approved_for_two_adopters`, `test_a_rival_can_be_approved_once_the_first_approval_is_reversed` |
| FR-7.9 | An illegal state transition MUST be refused and change nothing. | MUST | `test_illegal_move_raises`, `test_approving_twice_is_refused_rather_than_repeated`, `test_reversing_something_never_approved_is_refused` |

### FR-8 Invitations

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-8.1 | Staff MUST be able to send an invitation to an eligible adopter for a specific animal. | MUST | `test_invitation_is_recorded_with_a_seventy_two_hour_window` |
| FR-8.2 | An invitation MUST expire after the configured window, 72 hours by default (spec §7.4). | MUST | `test_expiry_is_exactly_seventy_two_hours_after_sending`, `test_a_configured_window_is_honoured`, `test_a_window_of_zero_is_refused` |
| FR-8.3 | An adopter MUST be able to accept or decline while the invitation is open, and MUST be refused afterwards. | MUST | `test_response_exactly_at_expiry_is_still_allowed`, `test_response_one_microsecond_after_expiry_is_refused` |
| FR-8.4 | Accepting an invitation MUST NOT approve an adoption. It starts an application. | MUST | `test_accepting_creates_an_application_not_an_approval` |
| FR-8.5 | Invitations MUST only be sent to adopters who opted in to proactive suggestions. | MUST | `test_opted_out_adopter_cannot_be_invited`, `test_opted_out_adopter_refused` |
| FR-8.6 | Every invitation state change MUST append its event, including expiry. | MUST | `test_overdue_invitation_is_expired_with_an_event` |
| FR-8.7 | Overdue invitations MUST be expired by the running application, not only by a test calling the command. | MUST | `test_an_overdue_invitation_is_expired_by_an_ordinary_request`, `test_the_sweep_does_not_run_again_on_the_next_request` |

### FR-9 Matching

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-9.1 | The system MUST compute an Adopter→Animal score for Find My Pet. | MUST | `test_adopter_sees_ranked_matches_with_scores` |
| FR-9.2 | The system MUST compute an Animal→Adopter score for Find My Adopter. | MUST | `test_staff_ranks_applicants_for_an_animal` |
| FR-9.3 | The two directions MUST use different weightings (spec §9). | MUST | `test_the_two_sets_differ_on_a_majority_of_criteria`, `test_same_pair_can_score_differently_by_direction` |
| FR-9.4 | Scoring MUST be deterministic: identical input gives identical output, with no LLM involvement (spec §8). | MUST | `test_repeated_scoring_of_the_same_pair_is_byte_identical`; `scripts/verify_requirements.py` scores the same pair twice with no model |
| FR-9.5 | Hard constraints MUST disqualify a candidate; soft preferences MUST only reduce the score. | MUST | `test_disqualification_scores_zero_and_carries_a_reason`, `test_species_mismatch_reduces_but_does_not_disqualify` |
| FR-9.6 | A deterministic eligibility filter MUST run before anyone is ranked for discovery (spec §10). | MUST | `tests/unit/test_invitation_rules.py::TestSendingEligibility`, `test_a_disqualified_adopter_is_named_with_their_reason` |
| FR-9.7 | Every match MUST be presented with reasons and concerns, never a bare number. | MUST | `test_every_criterion_supplies_an_explanation`, `test_an_empty_reasons_list_falls_back_to_the_criterion_text` |
| FR-9.8 | The eleven criteria of spec §8 MUST each be weighted in both directions, and each weight set MUST sum to 1. | MUST | `test_each_direction_sums_to_one`, `test_every_criterion_is_weighted_in_both_directions` |
| FR-9.9 | Every eligible match MUST carry a fit grade (A+ to F) whose itemised deductions add up to exactly 100 minus the score, each with its criterion's explanation. A disqualified match MUST carry none. | MUST | `test_deductions_add_up_to_exactly_what_the_score_is_missing`, `test_the_grade_never_contradicts_the_band`, `test_a_disqualified_pairing_shows_no_grade` |

### FR-10 The agent

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-10.1 | The agent MUST run as an independent OS process, not inside the Flask app (blueprint **§4 item 5** — the AI-agent item; §4.5 is Data Entry). | MUST | `scripts/verify_requirements.py`, check "AI agent, own process"; `test_agent_and_mcp_modules_never_import_the_web_tier` |
| FR-10.2 | The agent MUST obtain domain records through MCP tools over stdio, not by importing the application's web tier. | MUST | `test_both_mcp_tools_are_called`, `test_get_adopter_profile_round_trips` |
| FR-10.3 | The agent MUST consult the Vector DB via RAG as part of its assessment (blueprint §7). | MUST | `test_knowledge_base_is_consulted`, `test_retrieval_succeeds_with_no_keyword_overlap` |
| FR-10.4 | The agent MUST use web search only when the curated knowledge base cannot answer (spec §13). RAG sufficiency wins, even for an external topic. The guides count as answering only when a retrieved passage names the animal (its breed, or its species when no breed is recorded). | MUST | `test_no_search_when_knowledge_base_answers`, `test_agent_does_not_search_when_rag_suffices`, `test_generic_guidance_does_not_count_as_covering_a_breed`, `test_a_passage_naming_the_breed_keeps_the_web_shut` |
| FR-10.5 | The agent MUST NOT use web search to retrieve the application's own records. | MUST | `test_never_searches_for_the_applications_own_records`, `test_a_model_request_about_our_own_records_never_reaches_the_web` |
| FR-10.6 | The agent MUST return structured output: reasons, concerns, missing information, citations — with the score supplied deterministically. | MUST | `test_analysis_has_every_declared_field`, `test_wrong_schema_still_yields_a_storable_outcome` |
| FR-10.7 | The agent MUST NOT make the final adoption decision (spec §6.4). | MUST | `test_agent_never_names_a_decision_command`, `test_agent_exposes_no_decision_capability`, `test_the_outcome_carries_no_decision_field` |
| FR-10.8 | The agent MUST NOT state facts absent from its retrieved sources; a fabricated citation MUST be dropped. | MUST | `test_an_unknown_citation_is_dropped_and_recorded`, `test_a_reason_citing_an_unretrieved_source_is_not_stored` |
| FR-10.9 | Sources materially affecting an explanation MUST be recorded and displayable, and distinguishable from ones merely consulted. | MUST | `test_evidence_marks_only_the_sources_that_were_cited`, `test_the_stored_evidence_keeps_its_cited_flag` |
| FR-10.10 | A tool or model failure MUST degrade gracefully, not crash the loop. | MUST | `test_a_failing_model_degrades_to_the_deterministic_explanation`, `test_an_unexpected_exception_does_not_propagate_out_of_run_once`, `test_a_raising_retriever_becomes_missing_information` |
| FR-10.11 | The agent MUST choose its own tools rather than running a fixed script, and the loop MUST terminate by a real bound. | MUST | `test_the_manifest_is_sent_to_the_model_on_every_turn`, `test_a_scripted_rag_call_reaches_the_knowledge_base_with_its_query`, `test_the_step_cap_terminates_a_model_that_never_answers` |
| FR-10.12 | The steps the agent took MUST be auditable after the fact. | MUST | `test_the_loop_stops_when_the_model_answers_and_records_every_step`, `test_the_reasoning_trace_is_stored_as_json`, `test_the_reasoning_trace_and_cited_markers_are_rendered` |
| FR-10.13 | A job abandoned by a killed worker MUST return to the queue without an operator. | MUST | `test_a_stranded_job_returns_to_the_queue_and_runs`, `test_a_recently_claimed_job_is_left_alone` |

### FR-11 MCP tools

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-11.1 | The project MUST provide at least two local MCP tools communicating over stdio (blueprint §8). | MUST | `test_server_advertises_both_tools`; `scripts/verify_requirements.py`, check "Two local MCP tools (stdio)" |
| FR-11.2 | The tools MUST be `get_adopter_profile` and `get_animal_profile` (spec §14). | MUST | `test_get_adopter_profile_round_trips`, `test_get_animal_profile_round_trips` |
| FR-11.3 | Each tool MUST carry a description written for an LLM, and that description MUST be what the model actually receives. | MUST | `test_both_mcp_tools_appear_with_non_empty_descriptions` |
| FR-11.4 | MCP tools MUST be read-only. | MUST | `test_tools_expose_no_mutating_operation` |
| FR-11.5 | A missing record MUST return a structured not-found, never a protocol error. | MUST | `test_unknown_identifier_returns_structured_not_found`, `test_a_dead_mcp_subprocess_becomes_a_not_found_payload` |

### FR-12 Notifications

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-12.1 | The system MUST provide an internal inbox, readable by both roles. No email is required (spec §23). | MUST | `test_a_notification_is_shown`, `test_staff_have_an_inbox_too` |
| FR-12.2 | Notifications MUST be generated for invitations received, invitation responses and application status changes. | MUST | `test_sending_notifies_the_adopter` |
| FR-12.3 | One account's messages MUST NOT be readable or markable by another. | MUST | `test_the_inbox_shows_only_your_own_messages`, `test_marking_another_users_message_read_is_refused`, `test_marking_all_read_does_not_touch_another_user` |
| FR-12.4 | An unread count MUST be available on every page. | MUST | `test_the_count_is_available_on_any_page`, `test_an_anonymous_visitor_sees_zero` |

### FR-13 Event sourcing

| Id | Requirement | Priority | Proven by |
|---|---|---|---|
| FR-13.1 | Every meaningful state change MUST append an event carrying identifier, type, time, actor and data (blueprint §10). | MUST | `test_submission_records_event_and_queues_analysis`, `test_a_completed_job_writes_one_analysis_and_one_event`, `test_every_event_type_the_code_appends_is_in_the_enum` |
| FR-13.2 | The event log MUST be append-only; no UPDATE or DELETE. | MUST | `test_the_store_exposes_no_way_to_change_or_delete_an_event` |
| FR-13.3 | Current state MUST be reconstructible by replaying the log. | MUST | **Met.** `app/eventstore/projections.py`; `test_every_application_replays_to_its_stored_state_after_a_cascade`, `test_a_consistent_world_reports_no_mismatches`, `test_invitations_are_rebuilt_as_well_as_applications`. Scope: Applications and Invitations, the two event-sourced aggregates. |
| FR-13.4 | The system MUST be able to display relevant action history, to staff and to the adopter it concerns. | MUST | `test_an_adopter_may_read_their_own_application_history`, `test_staff_may_read_any_history` |
| FR-13.5 | Sequence numbers MUST be dense per aggregate, and a duplicate MUST be refused by the database. | MUST | `test_sequences_start_at_one_and_have_no_gaps`, `test_a_duplicate_sequence_number_is_refused_by_the_database` |

---

## 2. Non-functional requirements

### NFR-1 Architecture

| Id | Requirement | Proven by |
|---|---|---|
| NFR-1.1 | The backend MUST be Flask (blueprint §9). | `scripts/verify_requirements.py`, check "Flask" |
| NFR-1.2 | The system MUST follow MVC with no mixing of layer responsibilities. | `tests/unit/test_architecture_guard.py` |
| NFR-1.3 | Commands and queries MUST be separated conceptually and in implementation. | `scripts/verify_requirements.py`, check "MVC + CQRS" — 18 command and 19 query handlers on the bus |
| NFR-1.4 | A command MUST return only an identifier, a count of what it affected, or nothing; a query MUST NOT mutate state. | `test_command_handlers_never_return_read_data`, `test_query_modules_never_write` |
| NFR-1.5 | The domain layer MUST import no web framework and be testable with no app context. | `test_domain_layer_imports_no_framework_or_upper_layer` |
| NFR-1.6 | **A controller MUST NOT hold a database session.** The session is opened by the message bus and held by the command or query handler it dispatches to, for the length of one transaction. The domain layer holds none and imports no persistence library at all. | `test_controllers_never_hold_a_database_session`; `scripts/verify_requirements.py`, check "Controller boundary". *(This replaces an earlier "only repositories may hold a session": `app/repositories/` is an empty stub and there is no repository layer. `ARCHITECTURE.md` §2 and §3 say so.)* |
| NFR-1.7 | The agent MUST NOT import the Flask factory, a controller, a command, a query or the security layer; and no module under `app/` may import `agent_service`. | `test_agent_and_mcp_modules_never_import_the_web_tier`, `test_no_module_under_app_imports_the_language_model_client` |

### NFR-2 Data

| Id | Requirement | Proven by |
|---|---|---|
| NFR-2.1 | The transactional database MUST be cloud-hosted (blueprint §11). | `scripts/verify_requirements.py`, check "Cloud database"; `scripts/check_environment.py` against the live server |
| NFR-2.2 | Entities, relationships, keys and constraints MUST be explicitly defined. | `docs/MODEL_DATA.md` §2 against `app/infrastructure/models.py` |
| NFR-2.3 | The Vector DB MUST hold only curated knowledge, never transactional records. | `test_stored_chunk_count_matches_the_corpus`; the web-search gate's own-records rule |
| NFR-2.4 | The schema MUST work on SQL Server 2014, which has no native JSON type. | `test_the_stored_json_columns_decode_as_lists`, `tests/integration/test_qa_event_store.py::TestPayloadSerialisation` |
| NFR-2.5 | No stored value may exceed the column it is written to. | `test_every_animal_string_fits_its_column`, `tests/api/test_qa_input_validation.py::TestStringsLongerThanTheirColumn` |

### NFR-3 Performance

| Id | Requirement | Proven by |
|---|---|---|
| NFR-3.1 | No LLM call may occur in a request/response path. Measured CPU-only inference is 16 s for a JSON generation and 36–43 s for a tool-enabled turn, so ranking N candidates synchronously is not viable. | **Met.** The last synchronous call was in `/search/describe`; it now enqueues an `INTERPRET_INTENT` job and redirects. `test_describing_a_search_does_not_call_a_model_in_the_request`, `test_a_description_enqueues_exactly_one_job`, and structurally `test_no_module_under_app_imports_the_language_model_client` |
| NFR-3.2 | Ranking MUST be computed by the deterministic scorer and returned immediately; explanations MUST be generated asynchronously. | `test_the_ranking_screens_call_no_model`, `test_adopter_sees_ranked_matches_with_scores` |
| NFR-3.3 | Completed analyses MUST be cached and not recomputed for unchanged inputs. | **Met.** `test_a_repeated_job_for_unchanged_records_reuses_the_analysis`, `test_a_changed_animal_forces_recomputation_into_the_same_row`, `test_a_deterministic_fallback_row_is_regenerated_in_place` |
| NFR-3.4 | A search results page SHOULD return within 2 seconds on the cloud database. | Not asserted by a test. Observed in use; a timing assertion against a throttled free tier would be a flake generator, and saying so is better than pointing at a test that does not exist. |
| NFR-3.5 | A page waiting on the agent MUST NOT poll indefinitely: polling stops when nothing is pending and pauses while the tab is hidden. | `tests/api/test_feature_analysis_status.py::TestTheGeneration` for the signal the poller reads |

### NFR-4 Security

| Id | Requirement | Proven by |
|---|---|---|
| NFR-4.1 | Secrets MUST live in `.env`, which MUST be gitignored. No credential may appear in the repository or in documentation. | Review; `.env.example` carries placeholders only |
| NFR-4.2 | Passwords MUST be hashed with a salted algorithm. | `test_the_stored_password_is_hashed` |
| NFR-4.3 | Forms MUST be protected against CSRF. | **Met.** `CSRFProtect` on the application, a token in every POST form, `SameSite=Lax` and `HttpOnly` on the session cookie. `test_a_post_without_a_csrf_token_is_rejected`, `test_every_post_form_carries_a_token`, `test_the_session_cookie_is_marked_samesite`, `test_the_session_cookie_is_http_only` |
| NFR-4.4 | User-supplied content MUST be escaped on output. | `test_a_script_payload_in_the_search_box_comes_back_escaped`, `test_a_script_payload_stored_in_a_profile_is_escaped_when_shown`, `test_no_template_disables_autoescaping` |
| NFR-4.5 | Outbound HTTPS MUST work behind the corporate TLS-inspecting proxy via the OS trust store. | `scripts/check_environment.py`; `truststore.inject_into_ssl()` at import time in `app/config.py` and in `agent_service/__main__.py` |
| NFR-4.6 | No redirect target taken from a request or from stored data may leave the application. | `test_an_off_site_next_target_is_refused`, `test_an_off_site_target_is_refused` |
| NFR-4.7 | A malformed identifier or query string MUST NOT produce a server error. | `test_a_malformed_identifier_is_not_a_server_error`, `test_a_sql_payload_in_the_search_term_is_treated_as_text` |

### NFR-5 Testing

| Id | Requirement | Proven by |
|---|---|---|
| NFR-5.1 | Unit, integration, API, agent and E2E tests MUST all be present (blueprint §17). | `scripts/verify_requirements.py`, check "All test categories" |
| NFR-5.2 | Every feature MUST have at least one negative test, and the server MUST refuse what the browser would have blocked. | 127 failure-scenario tests, counted by the same check; `docs/TESTING.md` §4 |
| NFR-5.3 | Tests MUST NOT assert on generated LLM wording, only on structure and constraints. | `docs/TESTING.md` §2.1; enforced by review |
| NFR-5.4 | Scoring tests MUST run with no LLM and no network. | `tests/unit/test_matching.py` and `tests/unit/test_qa_matching_edges.py` carry the `unit` marker and construct everything inline |
| NFR-5.5 | Each test MUST document what it proves. | Every docstring begins "Proves…"; sampled by `docs/TESTING.md` §3 |
| NFR-5.6 | Documentation MUST NOT name a test, path or class that does not exist. | `tests/unit/test_docs_reference_real_artifacts.py`; `scripts/verify_requirements.py`, check "Docs reference real artifacts" |

### NFR-6 Usability

| Id | Requirement | Proven by |
|---|---|---|
| NFR-6.1 | The interface MUST be responsive from 320px upward with no horizontal page scroll. | `test_layout_survives_a_phone_viewport`, `test_every_roster_cell_carries_its_column_name_on_a_phone` |
| NFR-6.2 | Status MUST be conveyed by text as well as colour. | `tests/e2e/test_accessibility.py` |
| NFR-6.3 | Every image MUST carry descriptive alternative text. | `test_screen_has_no_serious_violations` (axe image-alt) |
| NFR-6.4 | `prefers-reduced-motion` MUST be honoured. | Stylesheet review; not asserted by a test |
| NFR-6.5 | The interface MUST work in both light and dark colour schemes. | `test_screen_passes_in_dark_mode_too`, `test_dark_mode_renders` |
| NFR-6.6 | Focus MUST be visible on every control in the tab order, and every page MUST offer a skip link. | `test_focus_is_visible_on_every_control_in_the_tab_order`, `test_every_page_offers_a_skip_link_to_the_main_landmark` |

### NFR-7 Code quality

| Id | Requirement | Proven by |
|---|---|---|
| NFR-7.1 | `ruff check .` and `mypy --strict app agent_service mcp_server tests scripts` MUST both pass with no errors. | **Met.** Both are quality gates in `docs/TESTING.md` §8 and are run before every commit. |
| NFR-7.2 | Public functions MUST carry type annotations and Google-convention docstrings. | Ruff `ANN` and `D` rule sets; `mypy --strict` |
| NFR-7.3 | Nesting SHOULD NOT exceed three levels; guard clauses are required over nested happy paths. | Enforced by review. Ruff's `max-branches = 8` limits branch *count*, not depth — no tool enforces depth 3, and `docs/SKILLS_AND_RULES.md` says so rather than overstating it. |
| NFR-7.4 | Names MUST be descriptive and unabbreviated. | Ruff `N`; enforced by review |

---

## 3. Out of scope

Explicitly excluded, from spec §3:

- Multiple organizations, marketplace or tenancy features
- Payments or fees
- Veterinary record management
- Social networking between adopters
- Email or SMS delivery
- Native mobile applications
