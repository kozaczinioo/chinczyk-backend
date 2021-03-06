import asyncio
import json
import os
import random
import threading
import uuid
from datetime import datetime, timedelta
from typing import List, Union

import requests

import app.color
from .connection import Connection
from .game import Game
from .server_errors import ItsNotYourTurn


class Room:
    def __init__(self, room_id, number_of_players=4):
        self.winners = []  # !use normal id!
        self.id = room_id
        self.active_connections: List[Connection] = []
        self.is_game_on = False
        self.game: Union[None, Game] = None
        self.whos_turn: str
        self.number_of_players = number_of_players
        self.game_id: str
        self.timeout = self.get_timeout()
        self.timer = threading.Timer(self.timeout, self.next_person_async)

    def get_timeout(self):
        try:
            timeout = float(os.path.join(os.getenv('TIMEOUT_SECONDS')))
        except TypeError:
            timeout = 15  # if theres no env var
        return timeout

    def next_person_async(self):
        asyncio.run(self.next_person_move())
        asyncio.run(self.broadcast_json())

    async def append_connection(self, connection):
        connection.player.game_id = self.get_free_color()
        self.active_connections.append(connection)
        self.export_room_status()
        if len(self.active_connections) >= self.number_of_players and self.is_game_on is False:
            await self.start_game()

    def get_taken_game_ids(self) -> List[str]:
        taken_ids = [connection.player.game_id for connection in self.active_connections]
        return taken_ids

    def get_taken_ids(self) -> List[str]:
        taken_ids = [connection.player.id for connection in self.active_connections]
        return taken_ids

    def get_players_in_game_game_ids(self) -> List[str]:
        taken_ids = [connection.player.game_id for connection in self.active_connections if
                     connection.player.in_game is True]
        return taken_ids

    def get_player(self, id):
        player = next(
            connection.player for connection in self.active_connections if connection.player.id == id)
        return player

    def get_players_in_game_regular_ids(self):
        players = [connection.player.id for connection in self.active_connections if
                   connection.player.in_game is True]
        return players

    def get_players_regular_ids(self):
        players = [connection.player.id for connection in self.active_connections]
        return players

    def get_free_color(self):
        taken_ids = self.get_taken_game_ids()
        for color in app.color.Color:
            if color.value not in taken_ids:
                return color.value

    async def remove_connection(self, connection_with_given_ws):
        await self.remove_player_by_id(connection_with_given_ws.player.id)
        self.active_connections.remove(connection_with_given_ws)

        if len(self.get_players_in_game_game_ids()) <= 1 and self.is_game_on is True:
            await self.end_game()

    async def broadcast_json(self):
        for connection in self.active_connections:
            gs = self.get_game_state(connection.player.id)
            await connection.ws.send_text(gs)

    async def restart_game(self):
        self.export_score()
        await self.start_game()

    async def start_game(self):
        self.is_game_on = True
        self.winners = []
        self.whos_turn = self.draw_random_player_id()
        self.put_all_players_in_game()
        self.game = Game(self.get_players_in_game_game_ids())
        self.game_id = str(uuid.uuid4().hex)
        self.restart_timer()
        await self.broadcast_json()

    async def end_game(self):
        self.timer.cancel()
        self.export_score()
        self.is_game_on = False
        self.whos_turn = 0
        self.game = None
        self.put_all_players_out_of_game()

    async def restart_or_end_game(self):
        if len(self.active_connections) >= self.number_of_players:
            await self.restart_game()
        else:
            await self.end_game()
            await self.broadcast_json()

    async def remove_player_by_game_id(self, game_id):
        player = next(
            connection.player for connection in self.active_connections if connection.player.game_id == game_id)
        if self.game:
            self.game.remove_players_counters_from_regular_and_idle_fields(player.game_id)
            if self.whos_turn == player.game_id:
                await self.next_person_move()
            player.in_game = False
            self.export_room_status()

    async def remove_player_by_id(self, id):
        player = next(
            connection.player for connection in self.active_connections if connection.player.id == id)
        if self.game is not None:
            self.game.remove_players_counters_from_regular_and_idle_fields(player.game_id)

            if self.whos_turn == player.game_id:
                await self.next_person_move()
            player.in_game = False
            self.export_room_status()
            print(f"kicked player {player.id}")

    def put_all_players_in_game(self):
        for connection in self.active_connections[:self.number_of_players]:
            connection.player.in_game = True
            if connection.player.game_id is None:
                connection.player.game_id = self.get_free_color()

    def put_all_players_out_of_game(self):
        for connection in self.active_connections:
            connection.player.in_game = False

    async def handle_players_move(self, client_id, player_move):
        player = next(
            connection.player for connection in self.active_connections if connection.player.id == client_id)
        self.validate_its_players_turn(player.game_id)

        next_person_move = self.game.handle_players_move(player.game_id, player_move)
        if next_person_move:
            await self.next_person_move()
            await self.check_and_handle_player_full_finnish(player)

    async def next_person_move(self):
        self.restart_timer()
        current_player = self.whos_turn
        taken_ids = self.get_players_in_game_game_ids()
        if len(taken_ids) <= 1:
            await self.end_game()
        current_idx = taken_ids.index(current_player)
        try:
            next_person = taken_ids[current_idx + 1]
        except IndexError:
            next_person = taken_ids[0]

        self.whos_turn = next_person
        self.game.roll_the_dice()

    def get_game_state(self, client_id) -> str:
        if self.is_game_on:
            player = next(
                connection.player for connection in self.active_connections if connection.player.id == client_id)
            game_state = dict(is_game_on=self.is_game_on, my_color=player.game_id,
                              turn_id=self.game.turn_id,
                              whos_turn=str(self.whos_turn), dice=self.game.dice,
                              game_data=self.game.get_current_state(), nicks=self.get_nicks(),
                              timestamp=self.timestamp.isoformat())
        else:
            game_state = dict(is_game_on=self.is_game_on, nicks=self.get_nicks())

        return json.dumps(game_state)

    def draw_random_player_id(self):
        return random.choice(self.get_taken_game_ids())

    @property
    def get_stats(self):
        if self.is_game_on:
            stats = {'is_game_on': self.is_game_on,
                     "whos turn": self.whos_turn,
                     'number_of_players': self.number_of_players,
                     'players_ids': self.get_players_regular_ids(),
                     "number_of_connected_players": len(self.active_connections),
                     "regular": self.game.regular,
                     "finnish": self.game.finnish,
                     "idle": self.game.idle}
        else:
            stats = {'is_game_on': self.is_game_on,
                     'number_of_players': self.number_of_players,
                     'players_ids': self.get_players_regular_ids(),
                     "number_of_connected_players": len(self.active_connections)}
        return stats

    def get_nicks(self):
        nicks = {}
        if self.is_game_on:
            for connection in self.active_connections:
                if connection.player.in_game is True:
                    enemy_color = connection.player.game_id
                    nicks[enemy_color] = connection.player.nick
        else:
            for connection in self.active_connections[:4]:
                enemy_color = connection.player.game_id
                nicks[enemy_color] = connection.player.nick
        return nicks

    def validate_its_players_turn(self, player_id):
        if player_id != self.whos_turn:
            raise ItsNotYourTurn

    async def kick_player(self, player_id):
        await self.remove_player_by_id(player_id)

    async def check_and_handle_player_full_finnish(self, player):
        if len(self.game.finnish[player.game_id]) == 4:
            print(f"player {player.id} has finished")
            self.winners.append(player.id)
            await self.remove_player_by_game_id(player.game_id)
            if len(self.winners) == 4:
                await self.restart_or_end_game()
            await self.broadcast_json()

    def export_score(self):
        try:
            result = requests.post(url=os.path.join(os.getenv('EXPORT_RESULTS_URL'), "games/handle-results/chinczyk"),
                                   json=dict(roomId=self.id, results=self.winners))
            if result.status_code == 200:
                print("export succesfull")
            else:
                print("export failed: ", result.text, result.status_code)
                print(self.winners)
        except (KeyError, TypeError):
            print("failed to get EXPORT_RESULT_URL env var")

    def export_room_status(self):
        connectionsCount: int = len(self.active_connections)
        try:
            if self.is_game_on:
                is_in_game = self.get_players_in_game_regular_ids()
                for player in self.active_connections:
                    if player.player.game_id not in is_in_game and player.player.game_id in self.winners:
                        is_in_game.append(player.player.id)
            else:
                is_in_game = self.get_taken_ids()
            result = requests.post(
                url=os.path.join(os.getenv('EXPORT_RESULTS_URL'), "rooms/update-room-status"),
                json=dict(roomId=self.id, currentResults=self.winners, connectionsCount=connectionsCount,
                          activePlayers=is_in_game))

            if result.status_code == 200:
                print("export succesfull")
            else:
                print("export failed: ", result.text, result.status_code)
                print(self.winners)
        except Exception as e:
            print(e.__class__.__name__)
            print("failed to get EXPORT_RESULTS_URL env var")

    def restart_timer(self):
        self.timer.cancel()
        self.timer = threading.Timer(self.timeout, self.next_person_async)
        self.timer.start()
        self.timestamp = datetime.now() + timedelta(0, self.timeout)
