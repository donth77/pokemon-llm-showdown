from poke_env.player import Player


class MaxDamagePlayer(Player):
    """Always picks the move with the highest base power. Falls back to random."""

    def choose_move(self, battle) -> str:
        if battle.available_moves:
            best_move = max(
                battle.available_moves,
                key=lambda m: m.base_power,
            )
            if best_move.base_power > 0:
                return self.create_order(best_move)

        return self.choose_random_move(battle)
