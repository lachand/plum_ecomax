import asyncio
import json
import struct
import logging
import socket
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Constantes Protocolaires
DEST_ID = 1
SOURCE_ID = 100
CMD_READ_VAL = 0x43
CMD_WRITE_FORCE = 0x29

class PlumDevice:
    def __init__(self, ip, port=8899, password="0000", user="admin", map_file="device_map.json"):
        self.ip = ip
        self.port = port
        self.password = password
        self.user = user
        self.map_file = map_file
        self.params_map: Dict[str, Any] = {}
        self.session_id = 10
        self._data_cache = {} 

    def load_map(self):
        try:
            with open(self.map_file, 'r') as f:
                self.params_map = json.load(f)
        except Exception as e:
            logger.error(f"Erreur chargement map: {e}")

    # --- ENCODAGE / DECODAGE ---
    def _encode(self, value: Any, param_def: dict) -> bytes:
        ptype = param_def['type']
        exp = param_def['exponent']
        
        # Gestion des exposants (ex: 20.5 -> 205 si exponent=1)
        if ptype != "FLOAT" and isinstance(value, (int, float)) and exp != 0:
            value = int(round(value / (10 ** exp)))

        try:
            if ptype == "FLOAT": return struct.pack("<f", float(value))
            elif ptype in ["BYTE", "SHORT_INT", "BOOL"]: return struct.pack("B", int(value))
            elif ptype in ["INT", "WORD"]: return struct.pack("<h", int(value))
            elif ptype in ["DWORD", "LONG_INT"]: return struct.pack("<i", int(value))
            return None
        except: return None

    def _decode(self, data: bytes, param_def: dict) -> Any:
        ptype = param_def['type']
        exp = param_def['exponent']
        try:
            val = None
            if ptype == "FLOAT" and len(data) >= 4:
                val = struct.unpack("<f", data[:4])[0]
                val = round(val, 2)
            elif ptype in ["BYTE", "SHORT_INT", "BOOL"] and len(data) >= 1:
                val = data[0]
            elif ptype in ["INT", "WORD"] and len(data) >= 2:
                val = struct.unpack("<h", data[:2])[0]
            elif ptype in ["DWORD", "LONG_INT"] and len(data) >= 4:
                val = struct.unpack("<i", data[:4])[0]

            if val is not None and isinstance(val, (int, float)) and exp != 0:
                val = val * (10 ** exp)
                val = round(val, 2)
            return val
        except: return None

    # --- API ---
    async def get_value(self, slug: str, retries: int = 3) -> Any:
        param = self.params_map.get(slug)
        if not param: return None
        pid = param['id']

        for attempt in range(1, retries + 1):
            val = await asyncio.to_thread(self._sync_get_value, pid, param)
            if val is not None:
                self._data_cache[slug] = val # Mise en cache
                return val
            await asyncio.sleep(0.2 * attempt)
        
        # Retourne la dernière valeur connue si échec
        return self._data_cache.get(slug)

    async def set_value(self, slug: str, value: Any, password: str = None, user: str = None) -> bool:
        param = self.params_map.get(slug)
        if not param: return False
        
        # Utilisation des identifiants stockés si non fournis
        target_pass = password if password is not None else self.password
        target_user = user if user is not None else self.user
        
        pid = param['id']
        encoded = self._encode(value, param)
        if not encoded: return False

        user_bytes = (target_user.encode('utf-8') + b'\x00') if target_user else b'\x00'
        pass_bytes = (target_pass.encode('utf-8') + b'\x00') if target_pass else b'\x00'
        full_payload = user_bytes + pass_bytes + b'\x01' + struct.pack("<H", pid) + encoded

        for attempt in range(1, 4):
            if await asyncio.to_thread(self._sync_set_value, pid, full_payload):
                return True
            await asyncio.sleep(1.0)
        return False

    # --- WORKERS SYNCHRONES ---
    def _sync_get_value(self, pid: int, param: dict) -> Any:
        self.session_id = (self.session_id + 1) % 65000
        payload = struct.pack("<HB BH", self.session_id, 1, 1, pid)
        frame = self._build_frame(CMD_READ_VAL, payload)
        resp = self._socket_transaction(frame)
        
        if resp and len(resp) > 7:
            # Extraction simple sans vérif poussée pour l'exemple
            return self._decode(resp[7:], param)
        return None

    def _sync_set_value(self, pid: int, payload: bytes) -> bool:
        self.session_id = (self.session_id + 1) % 65000
        frame = self._build_frame(CMD_WRITE_FORCE, payload)
        resp = self._socket_transaction(frame)
        return resp is not None

    def _build_frame(self, cmd, payload):
        l_val = 5 + len(payload)
        header = struct.pack("<HHHB", l_val, DEST_ID, SOURCE_ID, cmd)
        body = header + payload
        chk = self._crc16(body)
        return b'\x68' + body + struct.pack(">H", chk) + b'\x16'
    
    def _crc16(self, data: bytes) -> int:
        crc = 0x0000
        poly = 0x1021
        for b in data:
            crc ^= (b << 8)
            for _ in range(8):
                if crc & 0x8000: crc = (crc << 1) ^ poly
                else: crc <<= 1
                crc &= 0xFFFF
        return crc

    def _socket_transaction(self, frame: bytes) -> Optional[bytes]:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect((self.ip, self.port))
            sock.send(frame)
            
            buffer = bytearray()
            start = time.time()
            while time.time() - start < 2.0:
                chunk = sock.recv(1024)
                if not chunk: break
                buffer.extend(chunk)
                if b'\x68' in buffer and buffer.endswith(b'\x16'):
                     # Recherche simplifiée du header 0x68
                     idx = buffer.find(b'\x68')
                     return buffer[idx+8:-3] # Retourne payload
            return None
        except:
            return None
        finally:
            if sock: sock.close()