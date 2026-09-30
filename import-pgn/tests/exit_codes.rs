//! The importers must not end with exit code 0 when the server rejected
//! games: `rookhub/explorer-sync` decides by the exit code whether a Lichess
//! month or a Lumbra version counts as cleanly imported.

use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    path::PathBuf,
    process::{Command, Output},
    thread,
};

/// Answers every request with `status` and `body` (until the test ends).
fn mock_endpoint(status: &'static str, body: &'static str) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let addr = listener.local_addr().expect("local addr");
    thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            let mut reader = BufReader::new(stream);
            let mut content_length = 0;
            loop {
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap_or(0) == 0 {
                    break;
                }
                let line = line.trim_end();
                if line.is_empty() {
                    break;
                }
                if let Some((name, value)) = line.split_once(':') {
                    if name.eq_ignore_ascii_case("content-length") {
                        content_length = value.trim().parse().unwrap_or(0);
                    }
                }
            }
            let mut request_body = vec![0; content_length];
            let _ = reader.read_exact(&mut request_body);
            let mut stream = reader.into_inner();
            let _ = write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
        }
    });
    format!("http://{addr}")
}

fn run(bin: &str, pgn_name: &str, pgn: &str, endpoint: &str) -> Output {
    let path: PathBuf =
        std::env::temp_dir().join(format!("import-pgn-{}-{pgn_name}", std::process::id()));
    fs::write(&path, pgn).expect("write pgn");
    let output = Command::new(bin)
        .arg("--endpoint")
        .arg(endpoint)
        .arg(&path)
        .env_remove("HTTP_PROXY")
        .env_remove("http_proxy")
        .env_remove("ALL_PROXY")
        .env_remove("all_proxy")
        .output()
        .expect("run importer");
    let _ = fs::remove_file(&path);
    output
}

const LICHESS_PGN: &str = r#"[Event "Rated Blitz game"]
[Site "https://lichess.org/abcdefgh"]
[UTCDate "2025.01.01"]
[White "alpha"]
[Black "beta"]
[Result "1-0"]
[WhiteElo "2000"]
[BlackElo "2000"]
[TimeControl "300+0"]

1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0
"#;

const MASTERS_PGN: &str = r#"[Event "Test Open"]
[Site "Wien"]
[Date "2000.01.01"]
[Round "1"]
[White "Alpha, A"]
[Black "Beta, B"]
[Result "1-0"]
[WhiteElo "2500"]
[BlackElo "2500"]

1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0
"#;

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

#[test]
fn lichess_rejected_batch_exits_3_with_summary() {
    let endpoint = mock_endpoint("400 Bad Request", "rejected date");
    let output = run(
        env!("CARGO_BIN_EXE_import-lichess"),
        "lichess-rejected.pgn",
        LICHESS_PGN,
        &endpoint,
    );
    assert_eq!(output.status.code(), Some(3), "stdout: {}", stdout(&output));
    assert!(
        stdout(&output).contains("games sent: 1, rejected batches: 1 (1 games)"),
        "stdout: {}",
        stdout(&output)
    );
}

#[test]
fn lichess_success_exits_0_with_summary() {
    let endpoint = mock_endpoint("200 OK", "");
    let output = run(
        env!("CARGO_BIN_EXE_import-lichess"),
        "lichess-ok.pgn",
        LICHESS_PGN,
        &endpoint,
    );
    assert_eq!(output.status.code(), Some(0), "stdout: {}", stdout(&output));
    assert!(
        stdout(&output).contains("games sent: 1, rejected batches: 0 (0 games)"),
        "stdout: {}",
        stdout(&output)
    );
}

#[test]
fn masters_rejected_game_exits_3() {
    let endpoint = mock_endpoint("400 Bad Request", "rejected rating");
    let output = run(
        env!("CARGO_BIN_EXE_import-masters"),
        "masters-rejected.pgn",
        MASTERS_PGN,
        &endpoint,
    );
    assert_eq!(output.status.code(), Some(3), "stdout: {}", stdout(&output));
    assert!(
        stdout(&output).contains("rejected: 1"),
        "stdout: {}",
        stdout(&output)
    );
}

#[test]
fn masters_duplicate_and_success_exit_0() {
    for (status, body, expected) in [
        (
            "400 Bad Request",
            "duplicate game",
            "imported: 0, duplicate: 1, rejected: 0",
        ),
        ("200 OK", "", "imported: 1, duplicate: 0, rejected: 0"),
    ] {
        let endpoint = mock_endpoint(status, body);
        let output = run(
            env!("CARGO_BIN_EXE_import-masters"),
            "masters-ok.pgn",
            MASTERS_PGN,
            &endpoint,
        );
        assert_eq!(output.status.code(), Some(0), "stdout: {}", stdout(&output));
        assert!(
            stdout(&output).contains(expected),
            "stdout: {}",
            stdout(&output)
        );
    }
}
