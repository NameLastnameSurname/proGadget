import asyncio
import websockets
import json
import math
import uuid
import time
import os

WIDTH, HEIGHT = 1280, 720
FPS = 60

# --- ФИЗИЧЕСКИЕ КОНСТАНТЫ ---
ACCELERATION = 0.5   # Как быстро разгоняется
FRICTION = 0.96      # Сопротивление среды (чем меньше число, тем быстрее остановка)
MAX_SPEED = 12       # Максимальная скорость
ROTATION_SPEED = 5   # Скорость поворота

class GameSession:
    def __init__(self, p1_sock, p2_socket, p1_id, p2_id):
        self.p1_sock = p1_sock
        self.p2_sock = p2_socket
        self.p1_id = p1_id
        self.p2_id = p2_id
        self.running = True
        
        # Добавили vx и vy (векторы скорости)
        self.state = {
            'p1': {'x': 100, 'y': HEIGHT//2, 'angle': 0, 'vx': 0, 'vy': 0, 'hp': 100, 'alive': True},
            'p2': {'x': WIDTH-100, 'y': HEIGHT//2, 'angle': 180, 'vx': 0, 'vy': 0, 'hp': 100, 'alive': True},
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
                self.running = False 

            if winner:
                await asyncio.sleep(3)
                self.running = False

    def update_physics(self):
        for pid, key in [(self.p1_id, 'p1'), (self.p2_id, 'p2')]:
            inp = self.inputs.get(pid, {})
            p = self.state[key]
            if not p['alive']: continue

            # 1. Поворот
            if inp.get('left'): p['angle'] -= ROTATION_SPEED
            if inp.get('right'): p['angle'] += ROTATION_SPEED

            # 2. Ускорение (изменение скорости)
            if inp.get('up'):
                rad = math.radians(p['angle'])
                p['vx'] += math.cos(rad) * ACCELERATION
                p['vy'] += math.sin(rad) * ACCELERATION

            # 3. Трение (инерция)
            p['vx'] *= FRICTION
            p['vy'] *= FRICTION

            # 4. Обновление координат
            p['x'] += p['vx']
            p['y'] += p['vy']

            # 5. Отскок от стен (пружинистые границы)
            if p['x'] < 0: 
                p['x'] = 0
                p['vx'] *= -0.5 # Отскок с потерей энергии
            elif p['x'] > WIDTH: 
                p['x'] = WIDTH
                p['vx'] *= -0.5

            if p['y'] < 0: 
                p['y'] = 0
                p['vy'] *= -0.5
            elif p['y'] > HEIGHT: 
                p['y'] = HEIGHT
                p['vy'] *= -0.5

            # Обработка стрельбы
            if inp.get('shoot_trigger'):
                rad = math.radians(p['angle'])
                # Пуля наследует немного скорости корабля (для реализма, опционально)
                bullet_vx = math.cos(rad) * 15 + p['vx'] * 0.2
                bullet_vy = math.sin(rad) * 15 + p['vy'] * 0.2
                
                self.state['bullets'].append({
                    'x': p['x'], 'y': p['y'],
                    'vx': bullet_vx, 'vy': bullet_vy,
                    'owner': key
                })
                self.inputs[pid]['shoot_trigger'] = False

        # Физика пуль
        for b in self.state['bullets'][:]:
            b['x'] += b['vx']; b['y'] += b['vy']
            if not (0 <= b['x'] <= WIDTH and 0 <= b['y'] <= HEIGHT):
                self.state['bullets'].remove(b); continue

            target_key = 'p2' if b['owner'] == 'p1' else 'p1'
            target = self.state[target_key]
            if target['alive'] and math.hypot(b['x'] - target['x'], b['y'] - target['y']) < 25:
                target['hp'] -= 10
                if target['hp'] <= 0: target['alive'] = False
                self.state['bullets'].remove(b)

connected_players = {}
active_games = []

async def game_lifecycle(game):
    await game.loop()
    print(f"[SERVER] Game finished. Cleaning up.")
    if game in active_games: active_games.remove(game)
    for ws, info in connected_players.items():
        if info['id'] in [game.p1_id, game.p2_id]:
            info['status'] = 'lobby'
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
                    connected_players[websocket] = {'id': player_id, 'name': data['name'], 'status': 'lobby'}
                    await send_player_list()
                elif m_type == 'GET_LIST':
                    await send_player_list()
                elif m_type == 'CHALLENGE':
                    target_id = data['target_id']
                    if websocket not in connected_players: continue
                    me_name = connected_players[websocket]['name']
                    
                    target_ws = None
                    for ws, info in connected_players.items():
                        if info['id'] == target_id: target_ws = ws; break
                    
                    if target_ws:
                        await target_ws.send(json.dumps({'type': 'CHALLENGE_REQ', 'from_id': player_id, 'from_name': me_name}))
                elif m_type == 'CHALLENGE_RESP':
                    challenger_id = data['target_id']
                    if data['accept']:
                        challenger_ws = None
                        for ws, info in connected_players.items():
                            if info['id'] == challenger_id: challenger_ws = ws; break
                        
                        if challenger_ws:
                            connected_players[websocket]['status'] = 'game'
                            connected_players[challenger_ws]['status'] = 'game'
                            msg = json.dumps({'type': 'GAME_START'})
                            await websocket.send(msg)
                            await challenger_ws.send(msg)
                            game = GameSession(challenger_ws, websocket, challenger_id, player_id)
                            active_games.append(game)
                            asyncio.create_task(game_lifecycle(game))
                        else:
                            await websocket.send(json.dumps({'type': 'CHALLENGE_REJECTED'})) # Если тот уже ушел
                    else:
                         # Отказ
                         pass 

                elif m_type == 'INPUT':
                    for game in active_games:
                        if game.p1_id == player_id or game.p2_id == player_id:
                            if data.get('shoot'): game.inputs[player_id]['shoot_trigger'] = True
                            game.inputs[player_id].update(data)
                            break
            except Exception as e:
                print(f"Error: {e}")
    except: pass
    finally:
        if websocket in connected_players: del connected_players[websocket]
        await send_player_list()

async def send_player_list():
    lobby_list = [{'id': i['id'], 'name': i['name']} for i in connected_players.values() if i['status'] == 'lobby']
    msg = json.dumps({'type': 'LOBBY_LIST', 'players': lobby_list})
    for ws in connected_players:
        if connected_players[ws]['status'] == 'lobby':
             try: await ws.send(msg)
             except: pass

async def main():
    port = int(os.environ.get("PORT", 10000))
    async with websockets.serve(handler, "0.0.0.0", port):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
