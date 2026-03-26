from poke_env.player import Player
from poke_env.battle.move import Move
from poke_env.battle.pokemon import Pokemon


def _type_effectiveness(move: Move, target: Pokemon) -> float:
    """Compute a rough effectiveness multiplier for a move against a target."""
    multiplier = 1.0
    if move.type is None:
        return multiplier
    for t in target.types:
        if t is not None:
            multiplier *= move.type.damage_multiplier(t)
    return multiplier


class SmartPlayer(Player):
    """Picks moves considering type effectiveness and STAB bonus."""

    def choose_move(self, battle) -> str:
        if battle.available_moves and battle.opponent_active_pokemon:
            opponent = battle.opponent_active_pokemon
            active = battle.active_pokemon

            def _score(move: Move) -> float:
                if move.base_power == 0:
                    return -1.0
                effectiveness = _type_effectiveness(move, opponent)
                stab = 1.5 if (active and move.type in active.types) else 1.0
                return move.base_power * effectiveness * stab

            best_move = max(battle.available_moves, key=_score)
            if _score(best_move) > 0:
                return self.create_order(best_move)

        return self.choose_random_move(battle)
