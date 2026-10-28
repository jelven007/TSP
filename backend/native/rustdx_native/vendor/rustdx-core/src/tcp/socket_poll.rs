//! Windows readiness waits keep cancellation independent of socket timeout granularity.
use std::{io, net::TcpStream, os::windows::io::AsRawSocket, time::Duration};
use windows_sys::Win32::Networking::WinSock::{
    POLLERR, POLLHUP, POLLNVAL, POLLRDNORM, POLLWRNORM, WSAGetLastError, WSAPOLLFD, WSAPoll,
};

pub(super) fn readable(
    socket: &TcpStream,
    timeout: impl FnMut() -> io::Result<Duration>,
) -> io::Result<()> {
    wait(socket, POLLRDNORM, timeout)
}

pub(super) fn writable(
    socket: &TcpStream,
    timeout: impl FnMut() -> io::Result<Duration>,
) -> io::Result<()> {
    wait(socket, POLLWRNORM, timeout)
}

fn wait(
    socket: &TcpStream,
    events: i16,
    mut timeout: impl FnMut() -> io::Result<Duration>,
) -> io::Result<()> {
    loop {
        let milliseconds = timeout()?.min(Duration::from_millis(20)).as_millis().max(1) as i32;
        let mut descriptor = WSAPOLLFD {
            fd: socket.as_raw_socket() as _,
            events,
            revents: 0,
        };
        // SAFETY: the borrowed TcpStream keeps its socket alive, and the
        // initialized descriptor remains writable for this synchronous call.
        // The caller owns the complete-frame gate, so no managed read/write
        // can consume readiness before the subsequent I/O operation.
        let result = unsafe { WSAPoll(&mut descriptor, 1, milliseconds) };
        if result < 0 {
            // SAFETY: WSAGetLastError reads this thread's immediately preceding
            // Winsock call result and has no pointer or lifetime requirements.
            let error = io::Error::from_raw_os_error(unsafe { WSAGetLastError() });
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        if descriptor.revents & POLLNVAL != 0 {
            return Err(io::Error::new(
                io::ErrorKind::NotConnected,
                "socket closed while waiting for readiness",
            ));
        }
        if result > 0 && descriptor.revents & (events | POLLERR | POLLHUP) != 0 {
            // Cancellation/deadline may have changed during the native wait.
            timeout()?;
            return Ok(());
        }
    }
}
