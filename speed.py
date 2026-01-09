import asyncio
import numpy as np

# An action gauge of >100 means the character is the first to act.
def compute_speed(
        allies: list[tuple[str, float, float, float]],
        enemies: list[tuple[str, float, float]],
        N_sample: int = int(1e6)):
    '''
    allies: list of tuples (name, start_gauge, end_gauge, speed)
    enemy: tuple (name, start_gauge, end_gauge)
    '''
    ally_start_gauge, ally_end_gauge = [], []
    enemy_start_gauge, enemy_end_gauge = [], []

    for i in range(len(allies)):
        # Ranges of allies' action gauges
        ally_start_gauge_lower = max(allies[i][1] - 0.5, 0)
        ally_start_gauge_upper = min(allies[i][1] + 0.5, 5)
        ally_end_gauge_lower = min(allies[i][2] - 0.5, 100)
        ally_end_gauge_upper = min(allies[i][2] + 0.5, 100)
        # Sample allies' action gauges
        ally_start_gauge.append(np.random.uniform(
            ally_start_gauge_lower, ally_start_gauge_upper, N_sample))
        ally_end_gauge.append(np.random.uniform(
            ally_end_gauge_lower, ally_end_gauge_upper, N_sample))

    for i in range(len(enemies)):
        # Ranges of enemies' action gauges
        enemy_start_gauge_lower = max(enemies[i][1] - 0.5, 0)
        enemy_start_gauge_upper = min(enemies[i][1] + 0.5, 5)
        enemy_end_gauge_lower = min(enemies[i][2] - 0.5, 100)
        enemy_end_gauge_upper = min(enemies[i][2] + 0.5, 100)
        # Sample enemies' action gauges
        enemy_start_gauge.append(np.random.uniform(
            enemy_start_gauge_lower, enemy_start_gauge_upper, N_sample))
        enemy_end_gauge.append(np.random.uniform(
            enemy_end_gauge_lower, enemy_end_gauge_upper, N_sample))

    enemy_info = []
    for i in range(len(enemies)):
        # Initialize enemy's speed bounds
        enemy_min_speed, enemy_max_speed = 0, float('inf')
        enemy_speed_cat = []

        for j in range(len(allies)):
            # Compute enemy's speed using Monte Carlo
            enemy_speed = (enemy_end_gauge[i] - enemy_start_gauge[i]) \
                    / (ally_end_gauge[j] - ally_start_gauge[j]) * allies[j][3]
            # Enemy's strict speed bounds (Now we use Monte Carlo)
            #min_speed = ally_speed * (enemy_end_lower - enemy_start_upper) \
            #        / (ally_end_upper - ally_start_lower)
            #max_speed = ally_speed * (enemy_end_upper - enemy_start_lower) \
            #        / (ally_end_lower - ally_start_upper)
            enemy_min_speed = max(enemy_min_speed, np.min(enemy_speed))
            enemy_max_speed = min(enemy_max_speed, np.max(enemy_speed))
            enemy_speed_cat.append(enemy_speed)

        # Filter out impossible speeds
        enemy_speed_cat = np.concatenate(enemy_speed_cat)
        enemy_speed_cat = enemy_speed_cat[np.where(
            (enemy_speed_cat <= enemy_max_speed) \
            & (enemy_speed_cat >= enemy_min_speed))]

        # Compute mean and median speeds
        mean = np.mean(enemy_speed_cat)
        med = np.median(enemy_speed_cat)
        # Most likely integer speed (mode of rounded samples)
        spd_int = np.rint(enemy_speed_cat).astype(np.int64)  # round to nearest int
        lo = spd_int.min()
        mode_int = (np.bincount(spd_int - lo).argmax() + lo)
        # Compute ally's minimum speed to act before this enemy
        ally_min_speed = enemy_max_speed / 0.95
        
        enemy_info.append((enemies[i], enemy_min_speed, enemy_max_speed,
                           mean, med, mode_int, ally_min_speed))

    return enemy_info

async def compute_speed_async(*args, **kwargs):
    return await asyncio.to_thread(compute_speed, *args, **kwargs)

# Calculate the probability that chara_1 acts before chara_2
def overtake_prob(v1, v2):
    # Compute the ratio of speeds
    r = v2 / v1

    # 4 possible outcomes (close-form solution dereived by ChatGPT):
    if r <= 19/20:
        return 1.0
    if r >= 20/19:
        return 0.0
    if r <= 1.0:
        p = -200 * r + 381 - 361 / (2 * r)
    else:
        p = 361 / 2 * r - 380 + 200 / r

    return p

if __name__ == '__main__':
    ally_1 = ('水马', 1, 56, 135)
    ally_2 = ('水琴', 1, 70, 170)
    ally_3 = ('水拳', 4, 58, 131)
    enemy_1 = ('朱茵', 1, 101)
    enemy_2 = ('盖儿', 1, 84)
    print(compute_speed([ally_1, ally_2, ally_3], [enemy_1, enemy_2], 
                        N_sample=int(1e6)))
    print(overtake_prob(100, 100))
    print(overtake_prob(95, 100))
    print(overtake_prob(100, 95))
    print(overtake_prob(240, 246))
    print(overtake_prob(246, 240))
