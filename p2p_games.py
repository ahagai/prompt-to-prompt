from typing import Optional, Union, Tuple, List, Callable, Dict

import matplotlib.pyplot as plt
import torch
import diffusers
from diffusers import StableDiffusionPipeline
import torch.nn.functional as nnf
import numpy as np
import abc
import ptp_utils
import seq_aligner
import random
import os
import transformers
import diffusers
from clip_similarity import ClipSimilarity
import torch.nn.functional as F

from PIL import Image
print(transformers.__version__) # should be transformers==4.26.0
print(diffusers.__version__) # should be diffusers==0.11.1

MY_TOKEN = '<replace with your token>'
LOW_RESOURCE = False
NUM_DIFFUSION_STEPS = 50
GUIDANCE_SCALE = 7.5
MAX_NUM_WORDS = 77
device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
model_id = "stabilityai/stable-diffusion-2-1"
model_base_id = "CompVis/stable-diffusion-v1-4"
ldm_stable = StableDiffusionPipeline.from_pretrained(model_id).to(device)
# ldm_stable("a b c")
print("done")
tokenizer = ldm_stable.tokenizer

# prompts = ["a man smiling over a mountain view",
#            "a man wearing a hat smiling over a mountain view"
#            ]


class LocalBlend:

    def __call__(self, x_t, attention_store):
        k = 1
        maps = attention_store["down_cross"][2:4] + attention_store["up_cross"][:3]
        maps = [item.reshape(self.alpha_layers.shape[0], -1, 1, 16, 16, MAX_NUM_WORDS) for item in maps]
        maps = torch.cat(maps, dim=1)
        maps = (maps * self.alpha_layers).sum(-1).mean(1) # 1. alpha zeros out irrlevent stap. 2. we sum it out over the last dim[tokens]. 3. then we average over all the heads
        mask = nnf.max_pool2d(maps, (k * 2 + 1, k * 2 + 1), (1, 1), padding=(k, k))
        mask = nnf.interpolate(mask, size=(x_t.shape[2:]))
        mask = mask / mask.max(2, keepdims=True)[0].max(3, keepdims=True)[0]
        mask = mask.gt(self.threshold)
        mask = (mask[:1] + mask[1:]).float()
        x_t = x_t[:1] + mask * (x_t - x_t[:1])
        return x_t

    def __init__(self, prompts: List[str], words: [List[List[str]]], threshold=.3):
        alpha_layers = torch.zeros(len(prompts), 1, 1, 1, 1, MAX_NUM_WORDS)
        for i, (prompt, words_) in enumerate(zip(prompts, words)):
            if type(words_) is str:
                words_ = [words_]
            for word in words_:
                ind = ptp_utils.get_word_inds(prompt, word, tokenizer)
                alpha_layers[i, :, :, :, :, ind] = 1
        self.alpha_layers = alpha_layers.to(device)
        self.threshold = threshold


class AttentionControl(abc.ABC):

    def step_callback(self, x_t):
        return x_t

    def between_steps(self):
        return

    @property
    def num_uncond_att_layers(self):
        return self.num_att_layers if LOW_RESOURCE else 0

    @abc.abstractmethod
    def forward(self, attn, is_cross: bool, place_in_unet: str):
        raise NotImplementedError

    def __call__(self, attn, is_cross: bool, place_in_unet: str):
        if self.cur_att_layer >= self.num_uncond_att_layers:
            if LOW_RESOURCE:
                attn = self.forward(attn, is_cross, place_in_unet)
            else:
                h = attn.shape[0]
                attn[h // 2:] = self.forward(attn[h // 2:], is_cross, place_in_unet)
        self.cur_att_layer += 1
        if self.cur_att_layer == self.num_att_layers + self.num_uncond_att_layers:
            self.cur_att_layer = 0
            self.cur_step += 1
            self.between_steps()
        return attn

    def reset(self):
        self.cur_step = 0
        self.cur_att_layer = 0

    def __init__(self):
        self.cur_step = 0
        self.num_att_layers = -1
        self.cur_att_layer = 0


class EmptyControl(AttentionControl):

    def forward(self, attn, is_cross: bool, place_in_unet: str):
        return attn


class AttentionStore(AttentionControl):

    @staticmethod
    def get_empty_store():
        return {"down_cross": [], "mid_cross": [], "up_cross": [],
                "down_self": [], "mid_self": [], "up_self": []}

    def forward(self, attn, is_cross: bool, place_in_unet: str):
        key = f"{place_in_unet}_{'cross' if is_cross else 'self'}"
        if attn.shape[1] <= 32 ** 2:  # avoid memory overhead
            self.step_store[key].append(attn)
        return attn

    def between_steps(self):

        if len(self.attention_store) == 0:
            self.attention_store = self.step_store
        else:
            for key in self.attention_store:
                for i in range(len(self.attention_store[key])):
                    self.attention_store[key][i] += self.step_store[key][i]
        self.step_store = self.get_empty_store()

    def get_average_attention(self):
        average_attention = {key: [item / self.cur_step for item in self.attention_store[key]] for key in
                             self.attention_store}
        return average_attention

    def reset(self):
        super(AttentionStore, self).reset()
        self.step_store = self.get_empty_store()
        self.attention_store = {}

    def __init__(self):

        super(AttentionStore, self).__init__()
        self.step_store = self.get_empty_store()
        self.attention_store = {}


class AttentionControlEdit(AttentionStore, abc.ABC):

    def step_callback(self, x_t):
        if self.local_blend is not None:
            x_t = self.local_blend(x_t, self.attention_store)
        return x_t

    def replace_self_attention(self, attn_base, att_replace):
        if att_replace.shape[2] <= 16 ** 2:
            return attn_base.unsqueeze(0).expand(att_replace.shape[0], *attn_base.shape)
        else:
            return att_replace

    @abc.abstractmethod
    def replace_cross_attention(self, attn_base, att_replace):
        raise NotImplementedError

    def forward(self, attn, is_cross: bool, place_in_unet: str):
        super(AttentionControlEdit, self).forward(attn, is_cross, place_in_unet)
        if is_cross or (self.num_self_replace[0] <= self.cur_step < self.num_self_replace[1]):
            h = attn.shape[0] // (self.batch_size)
            attn = attn.reshape(self.batch_size, h, *attn.shape[1:])
            attn_base, attn_repalce = attn[0], attn[1:]
            if is_cross:
                alpha_words = self.cross_replace_alpha[self.cur_step]
                attn_repalce_new = self.replace_cross_attention(attn_base, attn_repalce) * alpha_words + (
                            1 - alpha_words) * attn_repalce
                attn[1:] = attn_repalce_new
            else:
                attn[1:] = self.replace_self_attention(attn_base, attn_repalce)
            attn = attn.reshape(self.batch_size * h, *attn.shape[2:])
        return attn

    def __init__(self, prompts, num_steps: int,
                 cross_replace_steps: Union[float, Tuple[float, float], Dict[str, Tuple[float, float]]],
                 self_replace_steps: Union[float, Tuple[float, float]],
                 local_blend: Optional[LocalBlend]):
        super(AttentionControlEdit, self).__init__()
        self.batch_size = len(prompts)
        self.cross_replace_alpha = ptp_utils.get_time_words_attention_alpha(prompts, num_steps, cross_replace_steps,
                                                                            tokenizer).to(device)
        if type(self_replace_steps) is float:
            self_replace_steps = 0, self_replace_steps
        self.num_self_replace = int(num_steps * self_replace_steps[0]), int(num_steps * self_replace_steps[1])
        self.local_blend = local_blend


class AttentionReplace(AttentionControlEdit):

    def replace_cross_attention(self, attn_base, att_replace):
        # if self.mapper.shape[0] == 1 and (self.mapper[0] - torch.eye(77).to("cuda")).sum() < 1e-6:
        #     # print("weird, einsum redundant, it basically do attn_base[None, :]")
        #     print()
        #
        # else:
        #     print("normal i think")

        return torch.einsum('hpw,bwn->bhpn', attn_base, self.mapper)

    def __init__(self, prompts, num_steps: int, cross_replace_steps: float, self_replace_steps: float,
                 local_blend: Optional[LocalBlend] = None):
        super(AttentionReplace, self).__init__(prompts, num_steps, cross_replace_steps, self_replace_steps, local_blend)
        self.mapper = seq_aligner.get_replacement_mapper(prompts, tokenizer).to(device)


class AttentionRefine(AttentionControlEdit):

    def replace_cross_attention(self, attn_base, att_replace):
        attn_base_replace = attn_base[:, :, self.mapper].permute(2, 0, 1, 3)
        attn_replace = attn_base_replace * self.alphas + att_replace * (1 - self.alphas)
        return attn_replace

    def __init__(self, prompts, num_steps: int, cross_replace_steps: float, self_replace_steps: float,
                 local_blend: Optional[LocalBlend] = None):
        super(AttentionRefine, self).__init__(prompts, num_steps, cross_replace_steps, self_replace_steps, local_blend)
        self.mapper, alphas = seq_aligner.get_refinement_mapper(prompts, tokenizer)
        self.mapper, alphas = self.mapper.to(device), alphas.to(device)
        self.alphas = alphas.reshape(alphas.shape[0], 1, 1, alphas.shape[1])


class AttentionReweight(AttentionControlEdit):

    def replace_cross_attention(self, attn_base, att_replace):
        if self.prev_controller is not None:
            attn_base = self.prev_controller.replace_cross_attention(attn_base, att_replace)
        attn_replace = attn_base[None, :, :, :] * self.equalizer[:, None, None, :]
        return attn_replace

    def __init__(self, prompts, num_steps: int, cross_replace_steps: float, self_replace_steps: float, equalizer,
                 local_blend: Optional[LocalBlend] = None, controller: Optional[AttentionControlEdit] = None):
        super(AttentionReweight, self).__init__(prompts, num_steps, cross_replace_steps, self_replace_steps,
                                                local_blend)
        self.equalizer = equalizer.to(device)
        self.prev_controller = controller


def get_equalizer(text: str, word_select: Union[int, Tuple[int, ...]], values: Union[List[float],
                                                                                     Tuple[float, ...]]):
    if type(word_select) is int or type(word_select) is str:
        word_select = (word_select,)
    equalizer = torch.ones(len(values), 77)
    values = torch.tensor(values, dtype=torch.float32)
    for word in word_select:
        inds = ptp_utils.get_word_inds(text, word, tokenizer)
        equalizer[:, inds] = values
    return equalizer


from PIL import Image


def aggregate_attention(attention_store: AttentionStore, res: int, from_where: List[str], is_cross: bool, select: int):
    out = []
    attention_maps = attention_store.get_average_attention()
    num_pixels = res ** 2
    for location in from_where:
        for item in attention_maps[f"{location}_{'cross' if is_cross else 'self'}"]:
            if item.shape[1] == num_pixels:
                cross_maps = item.reshape(len(prompts), -1, res, res, item.shape[-1])[select]
                out.append(cross_maps)
    out = torch.cat(out, dim=0)
    out = out.sum(0) / out.shape[0]
    return out.cpu()


def show_cross_attention(attention_store: AttentionStore, res: int, from_where: List[str], select: int = 0, name=""):
    tokens = tokenizer.encode(prompts[select])
    decoder = tokenizer.decode
    attention_maps = aggregate_attention(attention_store, res, from_where, True, select)
    images = []
    for i in range(len(tokens)):
        image = attention_maps[:, :, i]
        image = 255 * image / image.max()
        image = image.unsqueeze(-1).expand(*image.shape, 3)
        image = image.numpy().astype(np.uint8)
        image = np.array(Image.fromarray(image).resize((256, 256)))
        image = ptp_utils.text_under_image(image, decoder(int(tokens[i])))
        images.append(image)
        # plt.imshow(image)
        # plt.show()
    ptp_utils.view_images(np.stack(images, axis=0), title=name)

    # Image.fromarray(np.stack(images, axis=0)).save(f"{name}")




def show_self_attention_comp(attention_store: AttentionStore, res: int, from_where: List[str],
                             max_com=10, select: int = 0):
    attention_maps = aggregate_attention(attention_store, res, from_where, False, select).numpy().reshape(
        (res ** 2, res ** 2))
    u, s, vh = np.linalg.svd(attention_maps - np.mean(attention_maps, axis=1, keepdims=True))
    images = []
    for i in range(max_com):
        image = vh[i].reshape(res, res)
        image = image - image.min()
        image = 255 * image / image.max()
        image = np.repeat(np.expand_dims(image, axis=2), 3, axis=2).astype(np.uint8)
        image = Image.fromarray(image).resize((256, 256))
        image = np.array(image)
        images.append(image)
    ptp_utils.view_images(np.concatenate(images, axis=1))


def run_and_display(prompts, controller, latent=None, run_baseline=False, generator=None):
    if run_baseline:
        print("w.o. prompt-to-prompt")
        images, latent = run_and_display(prompts, EmptyControl(), latent=latent, run_baseline=False, generator=generator)
        print("with prompt-to-prompt")
    images, x_t = ptp_utils.text2image_ldm_stable(ldm_stable, prompts, controller, latent=latent,
            num_inference_steps=NUM_DIFFUSION_STEPS, guidance_scale=GUIDANCE_SCALE, generator=generator,
                                                  low_resource=LOW_RESOURCE)
    ptp_utils.view_images(images, title=prompts[0])
    # for image in images:
    #     plt.imshow(image)
    #     plt.show()
    return images, x_t


# prompts = ["A painting of a squirrel eating a burger"]
# controller = AttentionStore()
# image, x_t = run_and_display(prompts, controller, latent=None, run_baseline=False, generator=g_cpu)
# show_cross_attention(controller, res=16, from_where=("up", "down"))

# prompts = ["A painting of a squirrel eating a burger",
#            "A painting of a lion eating a burger"]
#
#
#
# controller = AttentionReplace(prompts, NUM_DIFFUSION_STEPS, cross_replace_steps=.8, self_replace_steps=.4)
# _ = run_and_display(prompts, controller, latent=x_t, run_baseline=False)
    # show_cross_attention(controller, res=16, from_where=("up", "down", "mid"))


# prompts = ["A painting of a squirrel eating a burger",
#            "A painting of a lion eating a burger"]
# lb = LocalBlend(prompts, ("squirrel", "lion"))
# controller = AttentionReplace(prompts, NUM_DIFFUSION_STEPS,
#                               cross_replace_steps={"default_": 0.8, "lion": .4},
#                               self_replace_steps=0.4, local_blend=lb)
# _ = run_and_display(prompts, controller, latent=x_t, run_baseline=False)
#
#
# controller = AttentionReplace(prompts, NUM_DIFFUSION_STEPS,
#                               cross_replace_steps={"default_": 0.8, "lion": .4},
#                               self_replace_steps=0.4, local_blend=None)
# _ = run_and_display(prompts, controller, latent=x_t, run_baseline=False)


def set_seed(seed: int = 42) -> None:
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    # When running on the CuDNN backend, two further options must be set
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    # Set a fixed value for the hash seed
    os.environ["PYTHONHASHSEED"] = str(seed)
    print(f"Random seed set as {seed}")

print(os.getcwd())
print()
seed = 8888


#
# for seed in list(range(200)):
#     g_cpu = torch.Generator().manual_seed(seed)
#
#     print(seed)
#
#     prompts = ["close up face shot of a woman wearing an elegant venetian mask, beautiful, classic",
#                "close up face shot of a woman, beautiful, classic"]
#
#
#
#     controller = AttentionRefine(prompts, NUM_DIFFUSION_STEPS,
#                                  cross_replace_steps={"default_": 0.6},
#                                  self_replace_steps=.2, local_blend=None)
#
#     image, x_t = run_and_display(prompts, controller, latent=None, generator=g_cpu)
#     show_cross_attention(controller, res=16, from_where=("up", "down"), name=f"mask_simple/croos_attn_{seed}_w_o_local_blend")
#     print(image.shape)
#     Image.fromarray(np.hstack((image[0], image[1]))).save(f"/cnvrg/mask_simple/{seed}_w_o_local_blend.jpg")
#     Image.fromarray(image[0]).save(f"/cnvrg/mask_simple/{seed}_0_w_o_local_blend.jpg")
#     Image.fromarray(image[1]).save(f"/cnvrg/mask_simple/{seed}_1_w_o_local_blend.jpg")
#
#
#     prompts = prompts[::-1]
#
#     controller = AttentionRefine(prompts, NUM_DIFFUSION_STEPS,
#                                  cross_replace_steps={"default_": 0.6},
#                                  self_replace_steps=.2, local_blend=None)
#
#     image, x_t = run_and_display(prompts, controller, latent=None, generator=g_cpu)
#     show_cross_attention(controller, res=16, from_where=("up", "down"),
#                          name=f"mask_simple/croos_attn_{seed}_w_o_local_blend_reverse_order_of_prompts")
#     print(image.shape)
#     Image.fromarray(np.hstack((image[0], image[1]))).save(f"/cnvrg/mask_simple/{seed}_w_o_local_blend_reverse_order_of_prompts.jpg")
#     Image.fromarray(image[0]).save(f"/cnvrg/mask_simple/{seed}_0_w_o_local_blend_reverse_order_of_prompts.jpg")
#     Image.fromarray(image[1]).save(f"/cnvrg/mask_simple/{seed}_1_w_o_local_blend_reverse_order_of_prompts.jpg")
#
#
#



def general_generation_exp_pipe(seeds_arr, weights_arr):
    global GUIDANCE_SCALE
    # cross_replace_vals = 0.7
    for gc in [5, 7.5, 10, 15]:
        GUIDANCE_SCALE = gc
        for cross_replace_vals in [0.6, 0.8, 0.9]:
            for seed in seeds_arr:  # list(range(8, 100)):
                x_1 = None
                start_with_no_makeup = []

                start_with_makeup = []
                folder = f"reweight/{seed}_{cross_replace_vals}_{gc}_mask"
                if not os.path.exists(f"/cnvrg/{folder}"):
                    os.mkdir(f"/cnvrg/{folder}")

                diff_folder = f"{folder}/diff"
                if not os.path.exists(f"/cnvrg/{diff_folder}"):
                    os.mkdir(f"/cnvrg/{diff_folder}")
                g_cpu = torch.Generator().manual_seed(seed)

                for weight in weights_arr:  # , 0.6, 1, 3, 6, 10, 20, 50, 150]:
                    print(f"weight - {weight}")
                    prompts = [
                        "close up face shot of a woman , beautiful, classic",
                        "close up face shot of a woman wearing an elegant venetian mask, beautiful, classic"]
                    if x_1 is not None:
                        g_cpu.set_state(x_1)

                    equalizer = get_equalizer(prompts[0], ("mask",), (weight,))
                    print(equalizer[equalizer != 1])

                    controller = AttentionReweight(prompts, NUM_DIFFUSION_STEPS,
                                                   cross_replace_steps={"default_": cross_replace_vals},
                                                   self_replace_steps=.2, local_blend=None, equalizer=equalizer)

                    image, x_t = run_and_display(prompts, controller, latent=None, generator=g_cpu)
                    # show_cross_attention(controller, res=16, from_where=("up", "down"), name=f"{folder}/croos_attn_{seed}_w_o_local_blend_{weight}")

                    # Image.fromarray(np.hstack((image[0], image[1]))).save(f"/cnvrg/{folder}/{seed}_w_o_local_blend_{weight}.jpg")
                    # Image.fromarray(image[0]).save(f"/cnvrg/{folder}/{seed}_0_w_o_local_blend_{weight}.jpg")
                    if x_1 is not None and len(start_with_no_makeup) == 0:
                        diff = np.abs((image[1].astype("float32") - image[0].astype("float32"))).astype("uint8")
                        Image.fromarray(diff).save(f"{diff_folder}/diff_makeup_weight_{weight}.png")
                        im = np.vstack([image[0], diff])
                        start_with_no_makeup.append(im)
                        Image.fromarray(image[0]).save(f"{folder}/start_no_makeup.png")

                    # Image.fromarray(image[1]).save(f"/cnvrg/{folder}/{seed}_1_w_o_local_blend_{weight}.jpg")
                    if x_1 is not None:
                        diff = np.abs((image[1].astype("float32") - image[0].astype("float32"))).astype("uint8")
                        Image.fromarray(diff).save(f"{diff_folder}/diff_makeup_weight_{weight}.png")
                        im = np.vstack([image[1], diff])
                        start_with_no_makeup.append(im)
                        Image.fromarray(image[1]).save(f"{folder}/result_no_makeup_weight_{weight}.png")

                    diff = np.abs((image[1].astype("float32") - image[0].astype("float32"))).astype("uint8")
                    Image.fromarray(diff).save(f"{diff_folder}/diff_no_makeup_weight_{weight}.png")

                    prompts = prompts[::-1]

                    if x_1 is not None:
                        g_cpu.set_state(x_1)
                    else:
                        print("getting x_1")
                        x_1 = g_cpu.get_state().clone()
                        continue

                    equalizer = get_equalizer(prompts[0], ("makeup",), (weight,))
                    print(equalizer[equalizer != 1])
                    controller = AttentionReweight(prompts, NUM_DIFFUSION_STEPS,
                                                   cross_replace_steps={"default_": cross_replace_vals},
                                                   self_replace_steps=.2, local_blend=None, equalizer=equalizer)

                    image, x_t = run_and_display(prompts, controller, latent=None, generator=g_cpu)

                    # Image.fromarray(np.hstack((image[0], image[1]))).save(f"/cnvrg/{folder}/{seed}_w_o_local_blend_reverse_order_of_prompts_{weight}.jpg")
                    # Image.fromarray(image[0]).save(f"/cnvrg/{folder}/{seed}_0_w_o_local_blend_reverse_order_of_prompts_{weight}.jpg")
                    if len(start_with_makeup) == 0 and x_1 is not None:
                        diff = np.abs((image[1].astype("float32") - image[0].astype("float32"))).astype("uint8")
                        Image.fromarray(diff).save(f"{diff_folder}/diff_makeup_weight_{weight}.png")
                        im = np.vstack([image[0], diff])
                        start_with_makeup.append(im)
                        Image.fromarray(image[0]).save(f"{folder}/start_makeup.png")
                    # Image.fromarray(image[1]).save(f"/cnvrg/{folder}/{seed}_1_w_o_local_blend_reverse_order_of_prompts_{weight}.jpg")

                    if x_1 is not None:
                        diff = np.abs((image[1].astype("float32") - image[0].astype("float32"))).astype("uint8")
                        Image.fromarray(diff).save(f"{diff_folder}/diff_makeup_weight_{weight}.png")
                        im = np.vstack([image[1], diff])
                        start_with_makeup.append(im)
                        Image.fromarray(image[1]).save(f"{folder}/result_makeup_weight_{weight}.png")

                    diff = np.abs((image[1].astype("float32") - image[0].astype("float32"))).astype("uint8")
                    Image.fromarray(diff).save(f"{diff_folder}/diff_makeup_weight_{weight}.png")

                print(f"len(start with no makeup) = {len(start_with_no_makeup)}")
                np_im = np.hstack(start_with_no_makeup)
                im = Image.fromarray(np_im)
                im.save(f"/cnvrg/{folder}/{seed}_starting_without_makeup.jpg")

                print(f"len(start with makeup) = {len(start_with_makeup)}")

                np_im = np.hstack(start_with_makeup)
                im = Image.fromarray(np_im)
                im.save(f"/cnvrg/{folder}/{seed}_starting_with_makeup.jpg")
                x_1 = None

def choose_couples_by_clip_sim(prompts, seeds, weights, eq_val, max_num_of_images, cross_replace_vals = 0.6):
    clip_similarity_metric = ClipSimilarity()

    folder = "/cnvrg/couples/"
    images_arr =[]
    images_sim_arr = []
    if not os.path.exists(folder):
        os.mkdir(folder)
    for seed in seeds:
        print(seed)
        g_cpu = torch.Generator().manual_seed(seed)

        for weight in weights:
            print(f"weight - {weight}")

            equalizer = get_equalizer(prompts[0], (eq_val,), (weight,))
            print(equalizer[equalizer != 1])

            controller = AttentionReweight(prompts, NUM_DIFFUSION_STEPS,
                                           cross_replace_steps={"default_": cross_replace_vals},
                                           self_replace_steps=.2, local_blend=None, equalizer=equalizer)

            image, x_t = run_and_display(prompts, controller, latent=None, generator=g_cpu)
            image_1 = Image.fromarray(image[0])
            image_2 = Image.fromarray(image[1])
            image_1.save(f"{folder}/{seed}_{weight}_0.png")
            image_2.save(f"{folder}/{seed}_{weight}_1.png")
            _, _, sim, sim_images = clip_similarity_metric(image_1, image_2, [prompts[0]], [prompts[1]])
            images_sim_arr.append(sim)
            images_arr.append((image_1, image_2))
            print(f"similarity as i want {sim}")
            print(f"similarity between images {sim_images}")

    images_sim_arr = torch.tensor(images_sim_arr)
    sorted_values, indices = images_sim_arr.sort(descending=True)
    for i, images in enumerate(images_arr):
        images_arr[i] = np.vstack(images)
    images_sorted_arr = []
    for i, ind in enumerate(indices):
        if i > max_num_of_images:
            break
        images_sorted_arr.append(images_arr[ind])
    images_sorted_arr = np.hstack(images_sorted_arr)
    print(images_sorted_arr.shape)
    Image.fromarray(images_sorted_arr).save(f"{folder}/sorted.png")

    print(indices)
    print(sorted_values)




def main():
    prompts = [
        "close up face photo, portrait, face centered  of a woman , beautiful, classic",
        "close up face photo, portrait, face centered of a woman wearing an elegant venetian mask, beautiful, classic"]
    eq_val = "makeup"
    samples = 15
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    start.record()
    choose_couples_by_clip_sim(prompts, list(range(samples)), [2], eq_val, max_num_of_images=20, cross_replace_vals=0.4)
    end.record()
    torch.cuda.synchronize()

    print(f"elapsed time in milliseconds for {samples} samples  - {start.elapsed_time(end)}")
    print(f"elapsed time in seconds for {samples} samples  - {start.elapsed_time(end) / 1000}")
    print(f"average time per sample in sec - {start.elapsed_time(end) / (1000 * samples)}")
    # general_generation_exp_pipe(list(range(10)), [0, 1, 2, 5])

if __name__ == "__main__":
    main()