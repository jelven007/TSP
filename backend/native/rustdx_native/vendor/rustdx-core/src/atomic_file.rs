//! 同目录临时文件写入，成功后原子替换；失败不覆盖上次结果。
use std::{
    fs::{self, File, OpenOptions},
    io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

static SEQUENCE: AtomicU64 = AtomicU64::new(0);

pub struct AtomicFile {
    destination: PathBuf,
    temporary: PathBuf,
}

impl AtomicFile {
    pub fn create(destination: impl AsRef<Path>) -> io::Result<(Self, File)> {
        let destination = destination.as_ref().to_owned();
        let filename = destination
            .file_name()
            .ok_or_else(|| io::Error::other("输出文件名为空"))?;
        let mut name = filename.to_os_string();
        name.push(format!(
            ".{}.{}.tmp",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let temporary = destination.with_file_name(name);
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        Ok((
            Self {
                destination,
                temporary,
            },
            file,
        ))
    }

    /// Read the staged bytes for verification before publication.
    pub fn temporary_path(&self) -> &Path {
        &self.temporary
    }

    pub fn commit(self) -> io::Result<()> {
        OpenOptions::new()
            .write(true)
            .open(&self.temporary)?
            .sync_all()?;
        fs::rename(&self.temporary, &self.destination)?;
        #[cfg(unix)]
        File::open(
            self.destination
                .parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or(Path::new(".")),
        )?
        .sync_all()?;
        Ok(())
    }
}

impl Drop for AtomicFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.temporary);
    }
}
