use std::{sync::Arc, time::SystemTime};

use amw_engine::store::session::{SessionKey, SessionStore, SessionStoreError};

#[test]
fn runtime_owned_arc_round_trips_isolated_session_snapshots() {
    let directory = tempfile::tempdir().unwrap();
    let store = Arc::new(SessionStore::open(directory.path().join("sessions")).unwrap());
    let alice = SessionKey::new("alice", [7; 32], "conversation_1").unwrap();
    let bob = SessionKey::new("bob", [7; 32], "conversation_1").unwrap();

    let reservation = store.reserve(&alice, 8).unwrap();
    store.write(reservation, b"kv-state").unwrap();

    assert_eq!(store.read(&alice).unwrap(), b"kv-state");
    assert!(matches!(store.read(&bob), Err(SessionStoreError::Unknown)));
    assert_eq!(store.list("alice", [7; 32]).unwrap().len(), 1);
    assert!(store.quota_status(Some("alice")).unwrap().saves_enabled);
    assert_eq!(
        store
            .sweep_expired(SystemTime::now())
            .unwrap()
            .sessions_deleted,
        0
    );
    store.delete(&alice).unwrap();
    assert!(store.list("alice", [7; 32]).unwrap().is_empty());
}
