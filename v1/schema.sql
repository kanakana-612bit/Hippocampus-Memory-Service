PRAGMA foreign_keys=ON;
PRAGMA application_id=1212372529;
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE raw_sources(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,name TEXT NOT NULL,head_snapshot_id TEXT,
 access_scope TEXT NOT NULL CHECK(access_scope IN ('public','personal','private','restricted')),
 destinations_json TEXT NOT NULL,timezone TEXT,version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(id,principal_id));
CREATE TABLE raw_snapshots(
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL,principal_id TEXT NOT NULL,raw_bytes BLOB NOT NULL,
 digest TEXT NOT NULL,message_count INTEGER NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(source_id,principal_id) REFERENCES raw_sources(id,principal_id));
CREATE TABLE raw_log_messages(
 source_id TEXT NOT NULL,sequence INTEGER NOT NULL CHECK(sequence>0),principal_id TEXT NOT NULL,
 original_json TEXT NOT NULL,digest TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(source_id,sequence),UNIQUE(source_id,sequence,principal_id),
 FOREIGN KEY(source_id,principal_id) REFERENCES raw_sources(id,principal_id));
CREATE TABLE learning_runs(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,mode TEXT NOT NULL,manifest_json TEXT NOT NULL,
 status TEXT NOT NULL,release_status TEXT NOT NULL DEFAULT 'unpublished',version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(id,principal_id));
CREATE TABLE learning_batches(
 id TEXT PRIMARY KEY,run_id TEXT NOT NULL,principal_id TEXT NOT NULL,source_id TEXT NOT NULL,
 first_sequence INTEGER NOT NULL,last_sequence INTEGER NOT NULL,input_json TEXT NOT NULL,
 status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,UNIQUE(id,principal_id),
 FOREIGN KEY(run_id,principal_id) REFERENCES learning_runs(id,principal_id),
 FOREIGN KEY(source_id,principal_id) REFERENCES raw_sources(id,principal_id));
CREATE TABLE staging(
 id TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,principal_id TEXT NOT NULL,
 output_json TEXT,validation_json TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(batch_id,principal_id) REFERENCES learning_batches(id,principal_id));
CREATE TABLE run_context_maps(
 run_id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,generation_id TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(run_id,principal_id) REFERENCES learning_runs(id,principal_id),
 FOREIGN KEY(generation_id,principal_id) REFERENCES context_map_generations(id,principal_id));
CREATE TABLE learning_intervals(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,source_id TEXT NOT NULL,first_sequence INTEGER NOT NULL,
 last_sequence INTEGER NOT NULL,batch_id TEXT NOT NULL UNIQUE REFERENCES learning_batches(id),version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(source_id,principal_id) REFERENCES raw_sources(id,principal_id));
CREATE TABLE learning_checkpoints(
 source_id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,sequence INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(source_id,principal_id) REFERENCES raw_sources(id,principal_id));
CREATE TABLE records(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,principal_id TEXT NOT NULL,data_json TEXT NOT NULL CHECK(json_valid(data_json)),
 version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,release_id TEXT,
 UNIQUE(id,principal_id),FOREIGN KEY(release_id,principal_id) REFERENCES learning_runs(id,principal_id));
CREATE TABLE record_access_policies(
 record_id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,access_scope TEXT NOT NULL CHECK(access_scope IN ('public','personal','private','restricted')),
 destinations_json TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(record_id,principal_id) REFERENCES records(id,principal_id));
CREATE TABLE event_origins(
 event_id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,source_id TEXT NOT NULL,sequence INTEGER NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES raw_snapshots(id),version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(source_id,sequence),FOREIGN KEY(event_id,principal_id) REFERENCES records(id,principal_id),
 FOREIGN KEY(source_id,sequence,principal_id) REFERENCES raw_log_messages(source_id,sequence,principal_id));
CREATE TABLE event_time_revisions(
 id TEXT PRIMARY KEY,event_id TEXT NOT NULL,principal_id TEXT NOT NULL,data_json TEXT NOT NULL,
 previous_id TEXT REFERENCES event_time_revisions(id),reason TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,UNIQUE(id,event_id,principal_id),
 FOREIGN KEY(event_id,principal_id) REFERENCES records(id,principal_id));
CREATE TABLE event_time_heads(
 event_id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,revision_id TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(revision_id,event_id,principal_id) REFERENCES event_time_revisions(id,event_id,principal_id));
CREATE TABLE record_source_spans(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,record_id TEXT NOT NULL,event_id TEXT NOT NULL,
 event_version INTEGER NOT NULL CHECK(event_version=1),span_json TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(record_id,principal_id) REFERENCES records(id,principal_id),
 FOREIGN KEY(event_id,principal_id) REFERENCES records(id,principal_id));
CREATE TABLE contexts(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL,
 access_scope TEXT NOT NULL,destinations_json TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,UNIQUE(id,principal_id));
CREATE TABLE context_source_spans(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,context_id TEXT NOT NULL,event_id TEXT NOT NULL,
 event_version INTEGER NOT NULL CHECK(event_version=1),span_json TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(context_id,principal_id) REFERENCES contexts(id,principal_id),
 FOREIGN KEY(event_id,principal_id) REFERENCES records(id,principal_id));
CREATE TABLE context_map_generations(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,base_id TEXT REFERENCES context_map_generations(id),
 manifest_json TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,UNIQUE(id,principal_id));
CREATE TABLE context_map_heads(
 principal_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(generation_id,principal_id) REFERENCES context_map_generations(id,principal_id));
CREATE TABLE utterance_context_membership(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,generation_id TEXT NOT NULL,event_id TEXT NOT NULL,context_id TEXT NOT NULL,
 membership_weight REAL NOT NULL CHECK(membership_weight>=0 AND membership_weight<=1),confidence REAL CHECK(confidence>=0 AND confidence<=1),
 source TEXT NOT NULL CHECK(source IN ('manual','model_extraction','inherited')),evidence_json TEXT NOT NULL,assigned_at TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,UNIQUE(principal_id,generation_id,event_id,context_id),
 FOREIGN KEY(generation_id,principal_id) REFERENCES context_map_generations(id,principal_id),
 FOREIGN KEY(event_id,principal_id) REFERENCES records(id,principal_id),
 FOREIGN KEY(context_id,principal_id) REFERENCES contexts(id,principal_id));
CREATE TABLE memory_context_membership(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,generation_id TEXT NOT NULL,memory_id TEXT NOT NULL,target_version INTEGER NOT NULL,
 context_id TEXT NOT NULL,membership_weight REAL NOT NULL CHECK(membership_weight>=0 AND membership_weight<=1),
 confidence REAL CHECK(confidence>=0 AND confidence<=1),source TEXT NOT NULL CHECK(source IN ('manual','model_extraction','inherited')),
 evidence_json TEXT NOT NULL,assigned_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(principal_id,generation_id,memory_id,target_version,context_id),
 FOREIGN KEY(generation_id,principal_id) REFERENCES context_map_generations(id,principal_id),
 FOREIGN KEY(memory_id,principal_id) REFERENCES records(id,principal_id),
 FOREIGN KEY(context_id,principal_id) REFERENCES contexts(id,principal_id));
CREATE TRIGGER event_membership_kind BEFORE INSERT ON utterance_context_membership
 WHEN NOT EXISTS(SELECT 1 FROM records WHERE id=NEW.event_id AND kind='event')
 BEGIN SELECT RAISE(ABORT,'membership target must be event'); END;
CREATE TRIGGER memory_membership_kind BEFORE INSERT ON memory_context_membership
 WHEN NOT EXISTS(SELECT 1 FROM records WHERE id=NEW.memory_id AND kind IN ('memory','summary','episode','knowledge_candidate') AND version=NEW.target_version)
 BEGIN SELECT RAISE(ABORT,'membership target kind/version mismatch'); END;
CREATE TRIGGER event_origin_kind BEFORE INSERT ON event_origins
 WHEN NOT EXISTS(SELECT 1 FROM records WHERE id=NEW.event_id AND kind='event')
 BEGIN SELECT RAISE(ABORT,'origin target must be event'); END;
CREATE TABLE rooms(id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,name TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,UNIQUE(id,principal_id));
CREATE TABLE situation_events(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,surface_id TEXT NOT NULL,room_id TEXT,audience TEXT NOT NULL,
 data_json TEXT NOT NULL,expires_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(room_id,principal_id) REFERENCES rooms(id,principal_id));
CREATE TABLE current_situations(
 principal_id TEXT NOT NULL,surface_id TEXT NOT NULL,situation_id TEXT NOT NULL REFERENCES situation_events(id),version INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(principal_id,surface_id));
CREATE TABLE context_activation_modifier(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,context_id TEXT NOT NULL,condition_json TEXT NOT NULL,
 modifier REAL NOT NULL CHECK(modifier>=-1 AND modifier<=1),expires_at TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(context_id,principal_id) REFERENCES contexts(id,principal_id));
CREATE TABLE context_ethical_weights(
 context_id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,weights_json TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(context_id,principal_id) REFERENCES contexts(id,principal_id));
CREATE TABLE record_ethical_weights(
 record_id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,weights_json TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(record_id,principal_id) REFERENCES records(id,principal_id));
CREATE TABLE reprocessing_queue(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,event_id TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(event_id,principal_id) REFERENCES records(id,principal_id));
CREATE TABLE retrieval_runs(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,identity_json TEXT NOT NULL,selection_json TEXT NOT NULL,
 dependency_json TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE association_edges(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,source_id TEXT NOT NULL,target_id TEXT NOT NULL,
 weight REAL NOT NULL CHECK(weight>=0 AND weight<=1),provenance_json TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(principal_id,source_id,target_id),
 FOREIGN KEY(source_id,principal_id) REFERENCES records(id,principal_id),
 FOREIGN KEY(target_id,principal_id) REFERENCES records(id,principal_id));
CREATE TABLE receipts(
 id TEXT PRIMARY KEY,principal_id TEXT NOT NULL,command TEXT NOT NULL,input_digest TEXT NOT NULL,result_json TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE audit_events(sequence INTEGER PRIMARY KEY,event_json TEXT NOT NULL,event_hash TEXT NOT NULL,signature TEXT NOT NULL);
CREATE TABLE object_heads(table_name TEXT NOT NULL,key_json TEXT NOT NULL,row_digest TEXT NOT NULL,PRIMARY KEY(table_name,key_json));
CREATE TRIGGER immutable_audit_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER immutable_audit_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER immutable_event BEFORE UPDATE ON records WHEN OLD.kind='event' BEGIN SELECT RAISE(ABORT,'immutable original event'); END;
CREATE TRIGGER immutable_raw_message BEFORE UPDATE ON raw_log_messages BEGIN SELECT RAISE(ABORT,'immutable original raw message'); END;
CREATE TRIGGER immutable_raw_snapshot BEFORE UPDATE ON raw_snapshots BEGIN SELECT RAISE(ABORT,'immutable raw snapshot'); END;
CREATE TRIGGER immutable_time_revision BEFORE UPDATE ON event_time_revisions BEGIN SELECT RAISE(ABORT,'immutable time revision'); END;
CREATE INDEX record_owner_kind ON records(principal_id,kind,release_id);
CREATE INDEX event_source_sequence ON event_origins(principal_id,source_id,sequence);
CREATE INDEX event_membership_context ON utterance_context_membership(principal_id,generation_id,context_id);
CREATE INDEX memory_membership_context ON memory_context_membership(principal_id,generation_id,context_id);
CREATE VIRTUAL TABLE event_fts USING fts5(id UNINDEXED,principal_id UNINDEXED,content,tokenize='trigram');
