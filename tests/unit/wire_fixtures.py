"""Ground-truth frames from Plum's "Standard Transmission Protocols ed.15"
(spec 1.5.3.12 p.25 and the write acknowledgement), shared by the codec and
transport tests."""

# Response to cmd 0x43, session=0x003B (59), one block of 1 param (pid=16,
# status=0x05, value=00 E0 2B 46) then one block of 3 params (pid=147..149).
SPEC_READ_RESPONSE = bytes.fromhex(
    "68"  # start
    "2000"  # l_val = 0x0020 = 32
    "0000"  # dest
    "0100"  # src = 1 (boiler)
    "C3"  # func = READ_RESP
    "3B00"  # session = 59
    "02"  # n_blocks = 2
    "01"
    "1000"  # block0: n_params=1, first_pid=16
    "05"
    "00E02B46"  # status=5, value (4 bytes)
    "03"
    "9300"  # block1: n_params=3, first_pid=147
    "10"
    "0F000000"  # status, value
    "10"
    "10000000"  # status, value
    "10"
    "0A00"  # status, value (2 bytes -- a different type)
    "F14F"  # CRC
    "16"  # stop
)

SPEC_WRITE_OK_RESPONSE = bytes.fromhex("680600000001 00A9E5FC1216".replace(" ", ""))
