import jax
import jax.numpy as jnp
from blackjax.util import run_inference_algorithm
from blackjax.util import store_only_expectation_values

from sampler_comparison.util import *
from sampler_evaluation.evaluation.ess import calculate_ess

# awaiting switch to full pytree support
# make_transform = lambda model : lambda pos : jax.tree.map(lambda z, b: b(z), pos, model.default_event_space_bijector)


# produce a kernel that only stores the average values of the bias for E[x_2] and Var[x_2]
def with_only_statistics(
    model, alg, initial_state, rng_key, num_steps, incremental_value_transform=None
):

    if incremental_value_transform is None:
        incremental_value_transform = lambda x: jnp.array(
            [
                jnp.average(
                    jnp.square(
                        x[1] - model.sample_transformations["square"].ground_truth_mean
                    )
                    / (
                        model.sample_transformations[
                            "square"
                        ].ground_truth_standard_deviation
                        ** 2
                    )
                ),
                jnp.max(
                    jnp.square(
                        x[1] - model.sample_transformations["square"].ground_truth_mean
                    )
                    / model.sample_transformations[
                        "square"
                    ].ground_truth_standard_deviation
                    ** 2
                ),
                jnp.average(
                    jnp.square(
                        x[0]
                        - model.sample_transformations["identity"].ground_truth_mean
                    )
                    / (
                        model.sample_transformations[
                            "identity"
                        ].ground_truth_standard_deviation
                        ** 2
                    )
                ),
                jnp.max(
                    jnp.square(
                        x[0]
                        - model.sample_transformations["identity"].ground_truth_mean
                    )
                    / model.sample_transformations[
                        "identity"
                    ].ground_truth_standard_deviation
                    ** 2
                ),
            ]
        )

    memory_efficient_sampling_alg, transform = store_only_expectation_values(
        sampling_algorithm=alg,
        state_transform=lambda state: jnp.array(
            [
                # model.sample_transformations["identity"](state.position),
                # model.sample_transformations["square"](state.position),
                model.default_event_space_bijector(state.position),
                model.default_event_space_bijector(state.position) ** 2,
                model.default_event_space_bijector(state.position) ** 4,
            ]
        ),
        incremental_value_transform=incremental_value_transform,
    )

    return run_inference_algorithm(
        rng_key=rng_key,
        initial_state=memory_efficient_sampling_alg.init(initial_state),
        inference_algorithm=memory_efficient_sampling_alg,
        num_steps=num_steps,
        transform=transform,
        progress_bar=True,
    )[1]


# this follows the inference_gym tutorial: https://github.com/tensorflow/probability/blob/main/spinoffs/inference_gym/notebooks/inference_gym_tutorial.ipynb
def initialize_model(model, key):

    z = jax.random.normal(key=key, shape=(model.ndims,))

    # awaiting switch to full pytree support
    #   def random_initialization(shape, dtype):
    #     return jax.tree.map(lambda d, s: jax.random.normal(key=key, shape=s, dtype=d), dtype, shape)

    #   z = jax.tree.map(lambda d, b, s: random_initialization(b.inverse_event_shape(s), d),
    #                         model.dtype, model.default_event_space_bijector, model.event_shape)

    #   x = jax.tree.map(lambda z, b: b(z), z, model.default_event_space_bijector)

    return z


make_log_density_fn = lambda model: lambda z: (
    model.unnormalized_log_prob(model.default_event_space_bijector(z))
    + model.default_event_space_bijector.forward_log_det_jacobian(z, event_ndims=1)
)


def sampler_grads_to_low_error(
    sampler, model, num_steps, batch_size, key, pvmap=jax.vmap
):

    try:
        model.sample_transformations[
            "square"
        ].ground_truth_mean, model.sample_transformations[
            "square"
        ].ground_truth_standard_deviation
    except:
        raise AttributeError("Model must have E_x2 and Var_x2 attributes")

    keys = jax.random.split(key, batch_size)

    # this key is deliberately fixed to the same value: we want all chains to start at the same position, for different samplers
    init_keys = jax.random.split(jax.random.key(2), batch_size)

    initial_position = jax.vmap(lambda key: initialize_model(model, key))(init_keys)

    squared_errors, metadata = pvmap(
        lambda key, pos: sampler(
            model=model, num_steps=num_steps, initial_position=pos, key=key
        )
    )(
        keys,
        initial_position,
    )

    err_t_avg_x2 = jnp.median(squared_errors[:, :, 0], axis=0)
    _, grads_to_low_avg_x2, _ = calculate_ess(
        err_t_avg_x2,
        grad_evals_per_step=metadata["num_grads_per_proposal"].mean(),
    )

    err_t_max_x2 = jnp.median(squared_errors[:, :, 1], axis=0)
    _, grads_to_low_max_x2, _ = calculate_ess(
        err_t_max_x2,
        grad_evals_per_step=metadata["num_grads_per_proposal"].mean(),
    )

    err_t_avg_x = jnp.median(squared_errors[:, :, 2], axis=0)
    _, grads_to_low_avg_x, _ = calculate_ess(
        err_t_avg_x,
        grad_evals_per_step=metadata["num_grads_per_proposal"].mean(),
    )

    err_t_max_x = jnp.median(squared_errors[:, :, 3], axis=0)
    _, grads_to_low_max_x, _ = calculate_ess(
        err_t_max_x,
        grad_evals_per_step=metadata["num_grads_per_proposal"].mean(),
    )

    return (
        {
            "max_over_parameters": {
                "square": {
                    "error": err_t_max_x2,
                    "grads_to_low_error": grads_to_low_max_x2.item(),
                },
                "identity": {
                    "error": err_t_max_x,
                    "grads_to_low_error": grads_to_low_max_x.item(),
                },
            },
            "avg_over_parameters": {
                "square": {
                    "error": err_t_avg_x2,
                    "grads_to_low_error": grads_to_low_avg_x2.item(),
                },
                "identity": {
                    "error": err_t_avg_x,
                    "grads_to_low_error": grads_to_low_avg_x.item(),
                },
            },
            "num_tuning_grads": metadata["num_tuning_grads"].mean().item(),
            "L": metadata["L"].mean().item(),
            "step_size": metadata["step_size"].mean().item(),
        },
        squared_errors,
    )
