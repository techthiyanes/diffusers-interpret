import inspect
import warnings
from typing import List, Optional, Union, Dict, Any, Tuple

from tqdm.auto import tqdm

import torch
from diffusers import LDMTextToImagePipeline
from transformers import BatchEncoding, PreTrainedTokenizerBase

from diffusers_interpret import BasePipelineExplainer
from diffusers_interpret.utils import transform_images_to_pil_format


class LDMTextToImagePipelineExplainer(BasePipelineExplainer):
    def __init__(self, pipe: LDMTextToImagePipeline, verbose: bool = True):
        super().__init__(pipe=pipe, verbose=verbose)

    @property
    def tokenizer(self) -> PreTrainedTokenizerBase:
        self.pipe: LDMTextToImagePipeline
        return self.pipe.tokenizer

    def get_prompt_tokens_token_ids_and_embeds(self, prompt: Union[str, List[str]]) -> Tuple[List[List[str]], BatchEncoding, torch.Tensor]:
        self.pipe: LDMTextToImagePipeline
        text_input = self.pipe.tokenizer(prompt, padding="max_length", max_length=77, return_tensors="pt")
        text_embeddings = self.pipe.bert(text_input.input_ids.to(self.pipe.device))[0]
        tokens = [self.pipe.tokenizer.convert_ids_to_tokens(sample) for sample in text_input['input_ids']]
        return tokens, text_input, text_embeddings

    def _mimic_pipeline_call(
        self,
        text_input: BatchEncoding,
        text_embeddings: torch.Tensor,
        batch_size: int,
        height: Optional[int] = 512,
        width: Optional[int] = 512,
        num_inference_steps: Optional[int] = 50,
        guidance_scale: Optional[float] = 7.5,
        eta: Optional[float] = 0.0,
        generator: Optional[torch.Generator] = None,
        output_type: Optional[str] = 'pil',
        run_safety_checker: bool = True,
        n_last_inference_steps_to_consider: Optional[int] = None,
        get_images_for_all_inference_steps: bool = False
    ) -> Dict[str, Any]:
        # TODO: add description

        self.pipe: LDMTextToImagePipeline

        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

        # get unconditional embeddings for classifier free guidance
        if guidance_scale != 1.0:
            uncond_input = self.pipe.tokenizer([""] * batch_size, padding="max_length", max_length=77, return_tensors="pt")
            uncond_embeddings = self.pipe.bert(uncond_input.input_ids.to(self.pipe.device))[0]

        # get the initial random noise
        latents = torch.randn(
            (batch_size, self.pipe.unet.in_channels, height // 8, width // 8),
            generator=generator,
        )
        latents = latents.to(self.pipe.device)

        self.pipe.scheduler.set_timesteps(num_inference_steps)

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        accepts_eta = "eta" in set(inspect.signature(self.pipe.scheduler.step).parameters.keys())

        extra_kwargs = {}
        if accepts_eta:
            extra_kwargs["eta"] = eta

        def decode_latents(latents: torch.Tensor, pipe: LDMTextToImagePipeline) -> Tuple[torch.Tensor, Optional[bool]]:
            # scale and decode the image latents with vae
            latents = 1 / 0.18215 * latents
            image = pipe.vqvae.decode(latents)

            image = (image / 2 + 0.5).clamp(0, 1)
            image = image.permute(0, 2, 3, 1)

            has_nsfw_concept = None
            if run_safety_checker:
                image = image.detach().cpu().numpy()
                safety_cheker_input = pipe.feature_extractor(
                    pipe.numpy_to_pil(image), return_tensors="pt"
                ).to(pipe.device)
                image, has_nsfw_concept = pipe.safety_checker(
                    images=image, clip_input=safety_cheker_input.pixel_values
                )

            return image, has_nsfw_concept

        all_generated_images = [] if get_images_for_all_inference_steps else None
        for i, t in tqdm(
            enumerate(self.pipe.scheduler.timesteps),
            total=len(self.pipe.scheduler.timesteps),
            desc="Diffusion process",
            disable=not self.verbose
        ):

            if n_last_inference_steps_to_consider:
                if i + 1 < len(self.pipe.scheduler.timesteps) - n_last_inference_steps_to_consider:
                    torch.set_grad_enabled(False)
                else:
                    torch.set_grad_enabled(True)

            # decode latents
            if get_images_for_all_inference_steps:
                with torch.no_grad():
                    image, _ = decode_latents(latents=latents, pipe=self.pipe)
                    all_generated_images.append(image)

            if guidance_scale == 1.0:
                # guidance_scale of 1 means no guidance
                latents_input = latents
                context = text_embeddings
            else:
                # For classifier free guidance, we need to do two forward passes.
                # Here we concatenate the unconditional and text embeddings into a single batch
                # to avoid doing two forward passes
                latents_input = torch.cat([latents] * 2)
                context = torch.cat([uncond_embeddings, text_embeddings])

            # predict the noise residual
            noise_pred = self.pipe.unet(latents_input, t, encoder_hidden_states=context)["sample"]
            # perform guidance
            if guidance_scale != 1.0:
                noise_pred_uncond, noise_prediction_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_prediction_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            latents = self.pipe.scheduler.step(noise_pred, t, latents, **extra_kwargs)["prev_sample"]

        image, has_nsfw_concept = decode_latents(latents=latents, pipe=self.pipe)
        if all_generated_images:
            all_generated_images.append(image)

        if output_type == "pil":
            if all_generated_images:
                all_generated_images = transform_images_to_pil_format(all_generated_images, self.pipe)
                image = all_generated_images[-1]
            else:
                image = transform_images_to_pil_format([image], self.pipe)[0]

        return {
            "sample": image,
            "nsfw_content_detected": has_nsfw_concept,
            "all_samples_during_generation": all_generated_images
        }
