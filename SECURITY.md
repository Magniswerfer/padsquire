# Security

PadSquire runs a settings app on your own computer (a web server on 127.0.0.1), talks to the
controller over USB and Bluetooth, and can write the controller's and the receiver's firmware.
On macOS it starts a small administrator helper for the firmware steps. A flaw in any of these
matters, so please report it privately first.

## Reporting a vulnerability

Use GitHub's private reporting: on the repository page, open **Security**, then **Report a
vulnerability** ([GitHub's guide](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
Please don't open a public issue for it.

It helps to include:

- the PadSquire version (`padsquire --version`) and your operating system;
- what someone could do, and what they would need first (local access, a page open in the
  browser, a Bluetooth device nearby);
- the steps to reproduce it.

You'll get an answer within a week. Once a fix is released, the advisory is published with
credit to you, unless you'd rather not be named.

## What counts

For example:

- another website, another local user or a nearby Bluetooth device reaching the settings app or
  the macOS helper;
- a way to get past the firmware safety checks (writing without a verified backup, touching a
  reserved flash area, sending a command the tools refuse);
- the installer or the in-app update installing something other than PadSquire's release.

Bugs in the controller's own firmware are GameSir's to fix; a way PadSquire could make them
worse is still welcome here.

## Supported versions

Only the newest release gets fixes. The in-app update (Help page) or the installer brings you to it.
