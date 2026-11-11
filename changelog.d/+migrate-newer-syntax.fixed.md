`esf migrate` no longer rejects a file that uses syntax newer than the running Python, such as `except*` on Python 3.10. The compile check now runs only when the original file compiles.
