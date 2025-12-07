import asyncio
import websockets
import json
import math
import uuid
import time
import os

WIDTH, HEIGHT = 1280, 720
FPS = 60

class GameSession:
    def __init__(self, p1_sock, p2_socket, p1_id, p2_id):
        self.p1_sock = p1_sock
        self.p2_sock = p2_socket
        self.p1_id = p1_id
        self.p2_id = p2_id
        self.running = True
        self.state = {
            'p1': {'x': 100, 'y': HEIGHT//2, 'angle': 0, 'hp': 100, 'alive': True},
            'p2': {'x': WIDTH-100, 'y': HEIGHT//2, 'angle': 180, 'hp': 100, 'alive': True},
            'bullets': []
        }
        self.inputs = {p1_id: {}, p2_id: {}}

    async def loop(self):
        last_time = time.time()
        while self.running:
            dt = time.time() - last_time
            if dt < 1/FPS: await asyncio.sleep(1/FPS - dt)
            last_time = time.time()

            self.update_physics()
            
            winner = None
            if not self.state['p1']['alive']: winner = "Player 2 Wins!"
            if not self.state['p2']['alive']: winner = "Player 1 Wins!"

            data = json.dumps({'type': 'GAME_STATE', 'state': self.state, 'winner': winner})
            
            try:
                await self.p1_sock.send(data)
                await self.p2_sock.send(data)
            except:
                self.running = False # Разрыв соединения

            if winner:
                # Даем 3 секунды посмотреть на победу, потом завершаем цикл
                await asyncio.sleep(3)
                self.running = False

    def update_physics(self):
        for pid, key in [(self.p1_id, 'p1'), (self.p2_id, 'p2')]:
            inp = self.inputs.get(pid, {})
            p = self.state[key]
            if not p['alive']: continue

            if inp.get('left'): p['angle'] -= 5
            if inp.get('right'): p['angle'] += 5
            if inp.get('up'):
                rad = math.radians(p['angle'])
                p['x'] = max(0, min(WIDTH, p['x'] + math.cos(rad) * 5))
                p['y'] = max(0, min(HEIGHT, p['y'] + math.sin(rad) * 5))

            if inp.get('shoot_trigger'):
                rad = math.radians(p['angle'])
                self.state['bullets'].append({
                    'x': p['x'], 'y': p['y'],
                    'vx': math.cos(rad) * 15, 'vy': math.sin(rad) * 15,
                    'owner': key
                })
                self.inputs[pid]['shoot_trigger'] = False

        for b in self.state['bullets'][:]:
            b['x'] += b['vx']; b['y'] += b['vy']
            if not (0 <= b['x'] <= WIDTH and 0 <= b['y'] <= HEIGHT):
                self.state['bullets'].remove(b); continue

            target_key = 'p2' if b['owner'] == 'p1' else 'p1'
            target = self.state[target_key]
            if target['alive'] and math.hypot(b['x'] - target['x'], b['y'] - target['y']) < 20:
                target['hp'] -= 10
                if target['hp'] <= 0: target['alive'] = False
                self.state['bullets'].remove(b)

connected_players = {}
active_games = []

# --- НОВАЯ ФУНКЦИЯ ДЛЯ УПРАВЛЕНИЯ ЖИЗНЬЮ ИГРЫ ---
async def game_lifecycle(game):
    # Запускаем игру и ждем, пока self.running не станет False
    await game.loop()
    
    print(f"[SERVER] Game finished. Cleaning up.")
    
    # 1. Удаляем игру из списка активных
    if game in active_games:
        active_games.remove(game)
    
    # 2. Возвращаем игрокам статус 'lobby'
    # Проходим по всем подключенным сокетам
    for ws, info in connected_players.items():
        if info['id'] in [game.p1_id, game.p2_id]:
            info['status'] = 'lobby'
    
    # 3. Рассылаем обновленный список лобби всем
    await send_player_list()

async def handler(websocket):
    player_id = str(uuid.uuid4())
    print(f"[SERVER] New connection: {player_id}")
    
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                m_type = data.get('type')

                if m_type == 'LOGIN':
                    print(f"[SERVER] LOGIN: {data['name']} ({player_id})")
                    connected_players[websocket] = {
                        'id': player_id,
                        'name': data['name'],
                        'status': 'lobby'
                    }
                    await send_player_list()

                elif m_type == 'GET_LIST':
                    await send_player_list()

                elif m_type == 'CHALLENGE':
                    target_id = data['target_id']
                    if websocket not in connected_players: continue

                    me_name = connected_players[websocket]['name']
                    print(f"[SERVER] CHALLENGE: {me_name} -> {target_id}")

                    target_ws = None
                    for ws, info in connected_players.items():
                        if info['id'] == target_id:
                            target_ws = ws
                            break
                    
                    if target_ws:
                        print(f"[SERVER] Sending CHALLENGE_REQ to {target_id}")
                        await target_ws.send(json.dumps({
                            'type': 'CHALLENGE_REQ', 
                            'from_id': player_id, 
                            'from_name': me_name
                        }))

                elif m_type == 'CHALLENGE_RESP':
                    print(f"[SERVER] RESPONSE: {data}")
                    challenger_id = data['target_id']
                    accepted = data['accept']
                    
                    challenger_ws = None
                    for ws, info in connected_players.items():
                        if info['id'] == challenger_id:
                            challenger_ws = ws
                            break

                    if accepted and challenger_ws:
                        # Ставим статус 'game'
                        connected_players[websocket]['status'] = 'game'
                        connected_players[challenger_ws]['status'] = 'game'
                        
                        msg = json.dumps({'type': 'GAME_START'})
                        await websocket.send(msg)
                        await challenger_ws.send(msg)
                        
                        # Создаем игру
                        game = GameSession(challenger_ws, websocket, challenger_id, player_id)
                        active_games.append(game)
                        
                        # ЗАПУСКАЕМ ЧЕРЕЗ ОБЕРТКУ (Lifecycle)
                        asyncio.create_task(game_lifecycle(game))
                        
                    elif challenger_ws:
                        await challenger_ws.send(json.dumps({'type': 'CHALLENGE_REJECTED'}))

                elif m_type == 'INPUT':
                    # Ищем игру, в которой участвует игрок.
                    # Важно: теперь, когда старая игра удаляется из active_games, 
                    # мы гарантированно найдем только новую активную игру.
                    for game in active_games:
                        if game.p1_id == player_id or game.p2_id == player_id:
                            if data.get('shoot'): game.inputs[player_id]['shoot_trigger'] = True
                            game.inputs[player_id].update(data)
                            break
            
            except Exception as e:
                print(f"[SERVER] ERROR processing message: {e}")
                import traceback
                traceback.print_exc()

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in connected_players:
            del connected_players[websocket]
            print(f"[SERVER] Disconnected: {player_id}")
            # Если игрок вышел во время игры, игра завершится сама через try-except в loop
            await send_player_list()

async def send_player_list():
    lobby_list = []
    for ws, info in connected_players.items():
        if info['status'] == 'lobby':
            lobby_list.append({'id': info['id'], 'name': info['name']})
    
    msg = json.dumps({'type': 'LOBBY_LIST', 'players': lobby_list})
    for ws in connected_players:
        if connected_players[ws]['status'] == 'lobby':
            try: await ws.send(msg)
            except: pass

async def main():
    port = int(os.environ.get("PORT", 10000))
    print(f"Server started on port {port}")
    async with websockets.serve(handler, "0.0.0.0", port):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
