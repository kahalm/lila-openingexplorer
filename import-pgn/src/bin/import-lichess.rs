use std::{ffi::OsStr, fs::File, io, mem, ops::ControlFlow, path::PathBuf, thread, time::Duration};

use clap::Parser;
use indicatif::{ProgressBar, ProgressDrawTarget, ProgressStyle};
use pgn_reader::{KnownOutcome, RawTag, Reader, SanPlus, Visitor};
use serde::Serialize;
use serde_with::{DisplayFromStr, StringWithSeparator, formats::SpaceSeparator, serde_as};
use shakmaty::Color;
use time::OffsetDateTime;

/// Exit code when all files were read, but the server rejected at least one
/// batch (clap already uses 2 for usage errors). The server stops a batch at
/// the first bad game, so the rest of that batch is missing as well.
const EXIT_REJECTED: i32 = 3;

#[derive(Debug, Serialize, Copy, Clone)]
#[serde(rename_all = "camelCase")]
enum Speed {
    UltraBullet,
    Bullet,
    Blitz,
    Rapid,
    Classical,
    Correspondence,
}

impl Speed {
    /// Name as used by the API (`ultraBullet`, `bullet`, ...).
    fn name(self) -> &'static str {
        match self {
            Speed::UltraBullet => "ultraBullet",
            Speed::Bullet => "bullet",
            Speed::Blitz => "blitz",
            Speed::Rapid => "rapid",
            Speed::Classical => "classical",
            Speed::Correspondence => "correspondence",
        }
    }

    fn from_seconds_and_increment(seconds: u64, increment: u64) -> Speed {
        let total = seconds + 40 * increment;

        if total < 30 {
            Speed::UltraBullet
        } else if total < 180 {
            Speed::Bullet
        } else if total < 480 {
            Speed::Blitz
        } else if total < 1500 {
            Speed::Rapid
        } else if total < 21_600 {
            Speed::Classical
        } else {
            Speed::Correspondence
        }
    }

    fn from_bytes(bytes: &[u8]) -> Result<Speed, ()> {
        if bytes == b"-" {
            return Ok(Speed::Correspondence);
        }

        let mut parts = bytes.splitn(2, |ch| *ch == b'+');
        let seconds = btoi::btou(parts.next().ok_or(())?).map_err(|_| ())?;
        let increment = btoi::btou(parts.next().ok_or(())?).map_err(|_| ())?;
        Ok(Speed::from_seconds_and_increment(seconds, increment))
    }
}

struct Batch {
    filename: PathBuf,
    games: Vec<Game>,
}

impl Batch {
    fn last_month(&self) -> &str {
        self.games
            .last()
            .and_then(|g| g.date.as_deref())
            .unwrap_or("")
    }
}

/// Optional pre-filter, so that only the games worth keeping are sent to the
/// indexer. Speeds up imports and keeps the database small.
#[derive(Clone, Default)]
struct Filter {
    /// Skip games whose average rating (as used for the rating groups) is
    /// below this value.
    min_avg_rating: Option<u16>,
    /// Skip games with these speeds (API names, e.g. `bullet`).
    exclude_speeds: Vec<String>,
}

impl Filter {
    fn accepts(&self, game: &Game) -> bool {
        if let (Some(min), Some(white), Some(black)) =
            (self.min_avg_rating, game.white.rating, game.black.rating)
        {
            let avg = (u32::from(white) + u32::from(black)) / 2;
            if avg < u32::from(min) {
                return false;
            }
        }
        match game.speed {
            Some(speed) => !self.exclude_speeds.iter().any(|s| s == speed.name()),
            None => true,
        }
    }
}

struct Importer<'a> {
    tx: crossbeam::channel::Sender<Batch>,
    filename: PathBuf,
    batch_size: usize,
    progress: &'a ProgressBar,
    filter: Filter,

    batch: Vec<Game>,
}

#[serde_as]
#[derive(Default, Serialize, Debug)]
struct Game {
    variant: Option<String>,
    speed: Option<Speed>,
    fen: Option<String>,
    id: Option<String>,
    date: Option<String>,
    white: Player,
    black: Player,
    #[serde_as(as = "Option<DisplayFromStr>")]
    winner: Option<Color>,
    #[serde_as(as = "StringWithSeparator<SpaceSeparator, SanPlus>")]
    moves: Vec<SanPlus>,
}

#[derive(Default, Serialize, Debug)]
struct Player {
    name: Option<String>,
    rating: Option<u16>,
}

impl Importer<'_> {
    fn new(
        tx: crossbeam::channel::Sender<Batch>,
        filename: PathBuf,
        batch_size: usize,
        progress: &ProgressBar,
        filter: Filter,
    ) -> Importer<'_> {
        Importer {
            tx,
            filename,
            batch_size,
            batch: Vec::with_capacity(batch_size),
            progress,
            filter,
        }
    }

    pub fn send(&mut self) {
        let batch = Batch {
            filename: self.filename.clone(),
            games: mem::replace(&mut self.batch, Vec::with_capacity(self.batch_size)),
        };
        self.progress.set_message(batch.last_month().to_string());
        self.tx.send(batch).expect("send");
    }
}

impl Visitor for Importer<'_> {
    type Tags = Game;
    type Movetext = Game;
    type Output = ();

    fn begin_tags(&mut self) -> ControlFlow<Self::Output, Self::Tags> {
        ControlFlow::Continue(Game::default())
    }

    fn tag(
        &mut self,
        game: &mut Game,
        name: &[u8],
        value: RawTag<'_>,
    ) -> ControlFlow<Self::Output> {
        if name == b"White" {
            game.white.name = Some(value.decode_utf8().expect("White").into_owned());
        } else if name == b"Black" {
            game.black.name = Some(value.decode_utf8().expect("Black").into_owned());
        } else if name == b"WhiteElo" {
            if value.as_bytes() != b"?" {
                game.white.rating = Some(btoi::btoi(value.as_bytes()).expect("WhiteElo"));
            }
        } else if name == b"BlackElo" {
            if value.as_bytes() != b"?" {
                game.black.rating = Some(btoi::btoi(value.as_bytes()).expect("BlackElo"));
            }
        } else if name == b"TimeControl" {
            game.speed = Some(Speed::from_bytes(value.as_bytes()).expect("TimeControl"));
        } else if name == b"Variant" {
            game.variant = Some(value.decode_utf8().expect("Variant").into_owned());
        } else if name == b"Date" || name == b"UTCDate" {
            game.date = Some(value.decode_utf8().expect("Date").into_owned());
        } else if name == b"WhiteTitle" || name == b"BlackTitle" {
            if value.as_bytes() == b"BOT" {
                return ControlFlow::Break(());
            }
        } else if name == b"Site" {
            game.id = Some(
                String::from_utf8(
                    value
                        .as_bytes()
                        .rsplitn(2, |ch| *ch == b'/')
                        .next()
                        .expect("Site")
                        .to_owned(),
                )
                .expect("Site"),
            );
        } else if name == b"Result" {
            match KnownOutcome::from_ascii(value.as_bytes()) {
                Ok(outcome) => game.winner = outcome.winner(),
                Err(_) => return ControlFlow::Break(()),
            }
        } else if name == b"FEN" {
            if value.as_bytes() == b"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" {
                // https://github.com/ornicar/lichess-db/issues/40
                game.fen = None;
            } else {
                game.fen = Some(value.decode_utf8().expect("FEN").into_owned());
            }
        }
        ControlFlow::Continue(())
    }

    fn begin_movetext(&mut self, game: Game) -> ControlFlow<Self::Output, Self::Movetext> {
        if game.white.rating.is_none() || game.black.rating.is_none() || !self.filter.accepts(&game)
        {
            ControlFlow::Break(())
        } else {
            ControlFlow::Continue(game)
        }
    }

    fn san(&mut self, game: &mut Game, san: SanPlus) -> ControlFlow<Self::Output> {
        game.moves.push(san);
        ControlFlow::Continue(())
    }

    fn end_game(&mut self, game: Game) -> Self::Output {
        self.batch.push(game);
        if self.batch.len() >= self.batch_size {
            self.send();
        }
    }
}

#[derive(Default)]
struct Counts {
    games: u64,
    rejected_batches: u64,
    rejected_games: u64,
}

#[derive(Parser)]
struct Args {
    #[arg(long, default_value = "http://localhost:9002")]
    endpoint: String,
    #[arg(long, default_value = "200")]
    batch_size: usize,
    #[arg(long)]
    avoid_utc_hour: Vec<u8>,
    /// Skip games with an average rating below this value.
    #[arg(long)]
    min_avg_rating: Option<u16>,
    /// Skip games with this speed (repeatable, e.g. `--exclude-speed bullet`).
    #[arg(long)]
    exclude_speed: Vec<String>,
    pgns: Vec<PathBuf>,
}

fn main() -> Result<(), io::Error> {
    let args = Args::parse();
    let filter = Filter {
        min_avg_rating: args.min_avg_rating,
        exclude_speeds: args.exclude_speed.clone(),
    };

    let (tx, rx) = crossbeam::channel::bounded::<Batch>(50);

    let bg = thread::spawn(move || {
        let client = reqwest::blocking::Client::builder()
            .timeout(None)
            .build()
            .expect("client");
        let mut counts = Counts::default();

        while let Ok(batch) = rx.recv() {
            while args
                .avoid_utc_hour
                .contains(&OffsetDateTime::now_utc().hour())
            {
                println!("paused around this time ...");
                thread::sleep(Duration::from_secs(10 * 60));
            }

            let res = client
                .put(format!("{}/import/lichess", args.endpoint))
                .json(&batch.games)
                .send()
                .expect("send batch");

            counts.games += batch.games.len() as u64;
            if !res.status().is_success() {
                counts.rejected_batches += 1;
                counts.rejected_games += batch.games.len() as u64;
                println!(
                    "{:?}: {}: {} - {}",
                    batch.filename,
                    batch.last_month(),
                    res.status(),
                    res.text().expect("decode response")
                );
            }
        }
        counts
    });

    for arg in args.pgns {
        let file = File::open(&arg)?;
        let progress = ProgressBar::with_draw_target(
            Some(file.metadata()?.len()),
            ProgressDrawTarget::stdout_with_hz(4),
        )
        .with_style(
            ProgressStyle::with_template(
                "{spinner} {prefix} {msg} {wide_bar} {bytes_per_sec:>14} {eta:>7}",
            )
            .unwrap(),
        )
        .with_prefix(format!("{arg:?}"));
        let file = progress.wrap_read(file);

        let uncompressed: Box<dyn io::Read> = if arg.extension() == Some(OsStr::new("bz2")) {
            Box::new(bzip2::read::MultiBzDecoder::new(file))
        } else if arg.extension() == Some(OsStr::new("zst")) {
            Box::new(zstd::Decoder::new(file)?)
        } else {
            Box::new(file)
        };

        let mut reader = Reader::new(uncompressed);
        let mut importer =
            Importer::new(tx.clone(), arg, args.batch_size, &progress, filter.clone());
        reader.visit_all_games(&mut importer)?;
        importer.send();

        progress.finish();
    }

    drop(tx);
    let counts = bg.join().expect("bg join");
    println!(
        "games sent: {}, rejected batches: {} ({} games)",
        counts.games, counts.rejected_batches, counts.rejected_games
    );
    if counts.rejected_batches > 0 {
        std::process::exit(EXIT_REJECTED);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const ALL_SPEEDS: [Speed; 6] = [
        Speed::UltraBullet,
        Speed::Bullet,
        Speed::Blitz,
        Speed::Rapid,
        Speed::Classical,
        Speed::Correspondence,
    ];

    fn game(white: u16, black: u16, speed: Option<Speed>) -> Game {
        Game {
            speed,
            white: Player {
                name: None,
                rating: Some(white),
            },
            black: Player {
                name: None,
                rating: Some(black),
            },
            ..Game::default()
        }
    }

    /// The filter as `rookhub/explorer-sync` calls it.
    fn rookhub_filter() -> Filter {
        Filter {
            min_avg_rating: Some(1600),
            exclude_speeds: vec!["bullet".to_owned(), "ultraBullet".to_owned()],
        }
    }

    #[test]
    fn min_avg_rating_rounds_down_like_the_server_rating_groups() {
        // The server groups by (white + black) / 2 rounded down (`util::midpoint`):
        // 1599.5 is group 1400 there, so it must not pass a 1600 filter.
        let filter = rookhub_filter();
        assert!(!filter.accepts(&game(1600, 1599, None)));
        assert!(!filter.accepts(&game(1599, 1599, None)));
        assert!(filter.accepts(&game(1600, 1600, None)));
        assert!(filter.accepts(&game(1601, 1599, None)));
        assert!(filter.accepts(&game(2800, 400, None)));
    }

    #[test]
    fn default_filter_accepts_everything() {
        for speed in ALL_SPEEDS {
            assert!(Filter::default().accepts(&game(400, 400, Some(speed))));
        }
    }

    #[test]
    fn excludes_speeds_by_api_name() {
        let filter = rookhub_filter();
        for speed in ALL_SPEEDS {
            let expected = !matches!(speed, Speed::Bullet | Speed::UltraBullet);
            assert_eq!(
                filter.accepts(&game(2000, 2000, Some(speed))),
                expected,
                "{}",
                speed.name()
            );
        }
        // Games without a time control are kept.
        assert!(filter.accepts(&game(2000, 2000, None)));
    }

    #[test]
    fn speed_names_match_the_serialized_api_names() {
        for speed in ALL_SPEEDS {
            assert_eq!(
                serde_json::to_string(&speed).unwrap(),
                format!("\"{}\"", speed.name())
            );
        }
    }

    #[test]
    fn speed_from_time_control() {
        let speed = |tc: &[u8]| Speed::from_bytes(tc).unwrap().name();
        assert_eq!(speed(b"15+0"), "ultraBullet");
        assert_eq!(speed(b"60+0"), "bullet");
        assert_eq!(speed(b"120+1"), "bullet");
        assert_eq!(speed(b"180+0"), "blitz");
        assert_eq!(speed(b"300+3"), "blitz");
        assert_eq!(speed(b"600+0"), "rapid");
        assert_eq!(speed(b"1800+0"), "classical");
        assert_eq!(speed(b"-"), "correspondence");
        assert!(Speed::from_bytes(b"300").is_err());
    }
}
